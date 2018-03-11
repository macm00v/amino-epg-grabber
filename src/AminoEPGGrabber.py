#!/usr/bin/env python

"""
A XMLTV compatible EPG grabber for the Amino EPG.

The grabber should function for any provider that supplies IPTV from Glashart Media.
"""
from urllib.error import URLError

# Set program version
VERSION = "v0.6"

from datetime import datetime, date, timedelta
from lxml import etree
import pytz
import http.client
import http.cookiejar
import socket
import io
import gzip
import json
import pickle
import os
import time
import inspect
import sys
import urllib.request

#===============================================================================
# The internal data struture used in the AminoEPGGrabber to
# store the EPG data is as follows:
# (dict)
#    epgData
#        channelname:(dict)
#            programid:(dict)
#                starttime
#                stoptime
#                title
#                sub-title
#                desc
#                actors []
#                directors []
#                categories []
#===============================================================================

GRABBERDIR = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))

class AminoEPGGrabber(object):
    """
    Class AminoEPGGrabber implements the grabbing and processing
    functionality needed for generating an XMLTV guide from the
    supplied location.
    """
    def __init__(self):
        # Set up defaults
        self.epgServer = "w1.zt6.nl"
        self.api = ""
        self.mac = ""
        self.maxDays = 7
        self.details = True
        self.downloadlogo = False
        self.logoStore = None
        self.xmltvFile = "aminoepg.xml"
        self.databaseFile = "aminograbber.pkl"
        self.channelDict = {}
        self.genreDict = {}
        
        self._timezone = pytz.timezone("Europe/Amsterdam")
        self._epgdata = dict()
        self._xmltv = None
        self._epgConnection = None
        self._foundLogos = dict()
        
    #===============================================================================
    # Getters and setters
    #===============================================================================
    def set_timezone(self, timezoneName):
        """Set the timezone we are working in, by name"""
        self._timezone = pytz.timezone(timezoneName)
    
    def get_timezone(self):
        """Return the name of the currently set timezone"""
        return self._timezone.zone
    
    timezone = property(get_timezone, set_timezone)

    #===============================================================================
    # Public functions
    #===============================================================================
    def loadConfig(self, configFile):
        """Load the configuration from the given config file"""
        
        try:
            configTree = etree.parse(configFile)
            config = configTree.getroot()
            
            if config.tag != "AminoEpgConfig":
                print("The config.xml file does not appear to be a valid AminoEPGGrabber configuration document.", file=sys.stderr)
                sys.exit(1)
                
            # Try to read each config tag
            server = config.find("server")
            if server != None:
                value = server.text.strip()
                if value != "":
                    self.epgServer = value

            api = config.find("api")
            if api != None:
                value = api.text.lower()
                if value != "":
                    self.api = value

            mac = config.find("mac")
            if mac != None:
                value = mac.text.lower()
                if value != "":
                    self.mac = value
                                    
            maxdays = config.find("maxdays")
            if maxdays != None:
                try:
                    value = int(maxdays.text)
                    if value < 7: # Make sure only value < 7 are set (7 is default)
                        self.maxDays = value
                except ValueError:
                    pass # Invalid value, ignore
                
            grabdetails = config.find("grabdetails")
            if grabdetails != None:
                value = grabdetails.text.lower()
                if value == "false": # True is default, so override to false only
                    self.details = False
                    
            downloadlogo = config.find("downloadlogo")
            if downloadlogo != None:
                value = downloadlogo.text.lower()
                if value == "true": # False is default, so override to false only
                    self.downloadlogo = True
                    
                    if "location" in downloadlogo.attrib:
                        location = downloadlogo.attrib["location"].strip()
                        if location != "":
                            self.logoStore = location
                            
            xmltvfile = config.find("xmltvfile")
            if xmltvfile != None:
                value = xmltvfile.text.strip()
                if value != "":
                    self.xmltvFile = value
                    
            databasefile = config.find("databasefile")
            if databasefile != None:
                value = databasefile.text.strip()
                if value != "":
                    self.databaseFile = value
                    
            channellist = config.find("channellist")
            if channellist != None:
                # Channel list found, parse all entries
                channelDict = {}
                for channel in channellist.findall("channel"):
                    # Skip channels that are missing an 'id'
                    if "id" not in channel.attrib:
                        continue
                    
                    # Add channel to channelDict (overwriting existing entry0
                    channelDict[channel.attrib["id"].strip()] = channel.text.strip()
                
                # Replace default channel dict with loaded dict
                self.channelDict = channelDict

            genrelist = config.find("genres")
            if genrelist != None:
                # Genre list found, parse all entries
                genreDict = {}
                for genre in genrelist.findall("genre"):
                    # Skip genres that are missing an 'id'
                    if "id" not in genre.attrib:
                        continue
                    
                    # Add genre to genreDict (overwriting existing entry)
                    genreDict[genre.attrib["id"].strip()] = genre.text.strip()
                
                # Replace default genre dict with loaded dict
                self.genreDict = genreDict
                
            
        except etree.XMLSyntaxError as ex:
            print("Error parsing config.xml file: %s" % ex, file=sys.stderr)
            sys.exit(1) # Quit with error code
        except EnvironmentError as ex:
            print("Error opening config.xml file: %s" % ex, file=sys.stderr)
            sys.exit(1) # Quit with error code
    
    
    def loadDatabase(self):
        """
        This function will load a database file into memory.
        It will overwrite the current in-memory data
        """
        # Only load if file exists
        databaseFile = os.path.join(GRABBERDIR, self.databaseFile)
        if os.path.isfile(databaseFile):
            dbFile = open(databaseFile, "rb")
            self._epgdata = pickle.load(dbFile)
            dbFile.close()
            
        # Remove channels that are not in the channel list
        if len(self.channelDict) > 0:
            for channel in list(self._epgdata.keys()):
                if channel not in self.channelDict:
                    del self._epgdata[channel]
        
        # Determine current date
        today = date.today()
        
        # Remove programs that stopped before 'now'
        for _, programs in self._epgdata.items():
            for programId in list(programs.keys()):
                stopDate = datetime.strptime(programs[programId]["stoptime"][:8], "%Y%m%d").date()
                if stopDate < today:
                    # Remove program
                    del programs[programId]
                else:
                    # Set program as not grabbed
                    programs[programId]["grabbed"] = False
        
    def writeDatabase(self):
        """
        This function will write the current in-memory EPG data to
        a database file.
        NOTE: Programs not found in the downloaded EPG will not be saved!
        """
        # Clean up old data (programs that weren't grabbed)
        for _, programs in self._epgdata.items():
            for programId in list(programs.keys()):
                if "grabbed" not in programs[programId] or \
                not programs[programId]["grabbed"]:
                    del programs[programId]
        
        # Write dictionary to disk
        databaseFile = os.path.join(GRABBERDIR, self.databaseFile)
        dbFile = open(databaseFile, "wb")
        pickle.dump(self._epgdata, dbFile)
        dbFile.close()
        
    def grabEpg(self):
        """
        This function will grab the EPG data from the EPG server.
        If an existing database file was loaded, that data will be updated.
        """
        # Report settings to user
        print("Grabbing EPG using the following settings:")
        print("Server to download from: %s" % self.epgServer)
        print("Number days of to grab : %s" % self.maxDays)
        print("Detailed program info  : %s" % ("Yes" if self.details else "No"))
        print("Download channel logo  : %s" % ("Yes" if self.downloadlogo else "No"))
        print("Writing XMLTV file to  : %s" % self.xmltvFile)
        print("Using database file    : %s" % self.databaseFile)
        print("Grabbing EPG for %d channels." % len(self.channelDict))
        print("")
        
        # Grab EPG data for all days
        for grabDay in range(self.maxDays):
            for dayPart in range(0, 8):
                grabDate = date.today() + timedelta(days=grabDay)
                print("Grabbing", str(grabDate), "part", dayPart, end=' ')
                print("(day " + str(grabDay+1) + "/" + str(self.maxDays) + ")")
                
                try:
                    # Set up new connection to EPG server
                    self._epgConnection = http.client.HTTPConnection(self.epgServer)
            
                    # Get basic EPG
                    fileId = grabDate.strftime("%Y%m%d.") + str(dayPart)
                    requestUrl = "/epgdata/epgdata." + fileId + ".json.gz"
                    
                    try:
                        self._epgConnection.request("GET", requestUrl)
                        response = self._epgConnection.getresponse()
                        epgData = response.read()
                        response.close()
                        
                        if response.status != 200:
                            print("HTTP Error %s (%s). Failed on fileid %s." % (response.status,
                                                                                response.reason,
                                                                                fileId))
                            break # break loop, no more days
                        
                    except socket.error as error:
                        print("Failed to download '" + fileId + "'")
                        print("The error was:", error)
                        return False # Return with error
                    except http.client.CannotSendRequest as error:
                        print("Error occurred on HTTP connection. Connection lost before sending request.")
                        print("The error was:", error)
                        return False # Return with error
                    except http.client.BadStatusLine as error:
                        print("Error occurred on HTTP connection. Bad status line returned.")
                        print("The error was:", error)
                        return False # Return with error
                    
                    # Decompress and retrieve data
                    compressedStream = io.StringIO(epgData)
                    rawData = gzip.GzipFile(fileobj=compressedStream).read()
                    basicEpg = json.loads(rawData, "UTF-8")
                    
                    # Close StringIO
                    compressedStream.close()
                    
                    # Process basic EPG
                    self._processBasicEPG(basicEpg)
                
                finally:
                    # Make sure connection gets closed
                    self._epgConnection.close()
                    self._epgConnection = None
                
        return True # Return with success
    
    def grabEpg_solocoo(self):
        """
        This function will grab the EPG data from the solocoo-type EPG server.
        If an existing database file was loaded, that data will be updated.
        """
        ua = "Mozilla/5.0 (Linux armv7l) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/49.0.2623.112 Safari/537.36 " + \
                    "OPR/36.0.2128.0 OMI/4.8.0.66.Aniara2.20"
        headers = {"Referer": self.epgServer + "/client.html",
                   "User-Agent": ua}
        goodies = http.cookiejar.CookieJar()
        unknownGenres = {}
        emptyChannels = []
        director = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookiejar=goodies))
        
        try:
            # Login sequence
            req = urllib.request.Request(self.epgServer + "/login.aspx?zs=" + self.mac + "&zs2=" + self.mac, \
                                         headers=headers)
            resp = director.open(req)
            
            if resp.getcode() == 200:
                if resp.read().decode("utf-8") == "[0,[1,null]]":
                    print("Login succeeded!")
                else:
                    print("Login error!")
                    return False
            else:
                print("Error in response from server:", resp.getcode())
                
            # Get all channels

            req = urllib.request.Request(self.epgServer + "/api.aspx?mtd=epg&f_format=cl&cs=13&v=3",
                                         headers=headers)
            resp = director.open(req).read().decode("utf-8")
            rawData = json.loads(resp)[1]
            epgData = {}
            startOfDay = datetime.today().replace(hour=4, minute=20)
            start = str(int(startOfDay.timestamp() * 1000))
            end = str(int((startOfDay.timestamp() + timedelta(days=self.maxDays).total_seconds()) * 1000))
            for channel in rawData:
                if len(self.channelDict) > 0 and channel["title"] not in self.channelDict:
                    continue
                
                print("Processing channel:", channel["title"])
                epgData[channel["title"]] = {}
                req = urllib.request.Request(self.epgServer + "/api.aspx?mtd=epg&f_format=pg&v=3" + \
                                             "&n=32767" + \
                                             "&f=" + start + \
                                             "&t=" + end + \
                                             "&s=" + str(channel["stationid"]) + \
                                             "&cs=15067", headers=headers)
                resp = director.open(req).read().decode("utf-8")
                programList = json.loads(resp)[1][str(channel["stationid"])]
                for grabbedProgram in programList:
                    program = dict()
                    
                    # Mandatory items
                    try:
                        program["title"] = grabbedProgram["title"]
                        program["starttime"] = self._convertTimestamp(int(grabbedProgram["start"]) / 1000)
                        program["stoptime"] = self._convertTimestamp(int(grabbedProgram["end"]) / 1000)
                    except KeyError:
                        # Invalid item, skip program
                        continue
                    
                    # Optional items
                    if "description" in grabbedProgram:
                        program["desc"] = grabbedProgram["description"]
                        
                    if "age" in grabbedProgram:
                        if grabbedProgram["age"] == 1:
                            program["rating"] = "AL"
                        else:
                            program["rating"] = str(grabbedProgram["age"]) + "+"
                        
                    if "genre" in grabbedProgram:
                        program["categories"] = list()
                        for cat in grabbedProgram["genre"]:
                            try:
                                program["categories"].append(self.genreDict[str(cat)])
                            except:
                                print("Unknown genre ID", cat, "in channel", channel["title"])
                                print("Adding to unknown genres and looking up possible value...")
                                idx = grabbedProgram["genre"].index(cat)
                                req = urllib.request.Request(self.epgServer + "/api.aspx?mtd=epg&f_format=pg&v=2&n=1&f=" + \
                                      str(grabbedProgram["start"]) + "&t=" + str(grabbedProgram["end"]) + \
                                      "&s=" + str(channel["stationid"]) + "&cs=512", headers=headers)
                                resp = director.open(req).read().decode("utf-8")
                                oldApiPgm = json.loads(resp)[1][str(channel["stationid"])][0]
                                if "genre" in oldApiPgm and idx < len(oldApiPgm["genre"]):
                                    unknownGenres[str(cat)] = oldApiPgm["genre"][idx]
                                    print("Added unknown genre", cat, "=", unknownGenres[str(cat)])
                                else:
                                    unknownGenres[str(cat)] = "<unknown>"
                    
                    epgData[channel["title"]][grabbedProgram["locId"]] = program
                
                # Remove empty channels
                if len(epgData[channel["title"]]) == 0:
                    emptyChannels.append(channel["title"])
                    del epgData[channel["title"]]
                    

        except URLError as error:
            print("Error connecting to api:", error)

        self._epgdata = epgData
        
        if len(unknownGenres) != 0:
            print("Possible addition to genres in config.xml:")
            for idx, value in unknownGenres.items():
                print("    <genre id=\"" + str(idx) + "\">" + value + "</genre>")
            print("")
            
        if len(emptyChannels) != 0:
            print("The following channels appear to have no EPG data:")
            for value in emptyChannels:
                print("   ", value)
            print("")
        
        return True
                
    def writeXmltv(self):
        """
        This function will write the current in-memory EPG data to an XMLTV file.
        NOTE: Programs not found in the downloaded EPG will not be saved!
        """
        # Set up XML tree and create main <TV> tag
        self._xmltv = etree.Element("tv",
                                    attrib = {"source-info-url"     : self.epgServer,
                                              "source-info-name"    : "Local amino EPG server",
                                              "generator-info-name" : "AminoEPGGrabber %s (C) 2012 Jeroen Bogers" % VERSION,
                                              "generator-info-url"  : "http://gathering.tweakers.net"}
                                    )
        
        # Add channels to XML
        for channel in sorted(self._epgdata.keys()):
            channelTag = etree.Element("channel", id = channel)
            channelDisplayNameTag = etree.Element("display-name", lang = "nl")
            if channel in self.channelDict:
                channelDisplayNameTag.text = self.channelDict[channel]
            else:
                channelDisplayNameTag.text = channel
            channelTag.append(channelDisplayNameTag)
            
            # Add icon link, if available
            if channel in self._foundLogos:
                logoLink = "file://%s" % self._foundLogos[channel]
                channelIconTag = etree.Element("icon", src = logoLink)
                channelTag.append(channelIconTag)
                
            self._xmltv.append(channelTag)
            
        # Add programs to XML
        for channel, programs in sorted(self._epgdata.items()):
            for _, program in sorted(programs.items()):
                self._xmltv.append(self._getProgramAsElement(channel, program))
                
        # Write XMLTV file to disk
        xmltvFile = os.path.join(GRABBERDIR, self.xmltvFile)
        etree.ElementTree(element=self._xmltv).write(xmltvFile)
        
        
    #===============================================================================
    # Private functions
    #===============================================================================
    def _processBasicEPG(self, basicEpg):
        """
        Takes the loaded EPG data and converts it to the in-memory
        structure. If the program is not in memory, or differs from
        the in memory data, the details are retrieved.
        """
        for channel, grabbedPrograms in basicEpg.items():
            # Ignore channels not in the channel list (if given)
            if len(self.channelDict) > 0 and channel not in self.channelDict:
                continue
            
            # Check if data for channel is loaded yet
            if channel not in self._epgdata:
                self._epgdata[channel] = dict()
                
            # Check if channel icon needs to be downloaded
            if self.downloadlogo:
                self._getLogo(channel)
            
            # Store all program data
            for grabbedProgram in grabbedPrograms:
                # Convert to internal structure
                try:
                    programId = grabbedProgram["id"]
                    program = dict()
                    program["grabbed"] = True
                    program["starttime"] = self._convertTimestamp(grabbedProgram["start"])
                    program["stoptime"] = self._convertTimestamp(grabbedProgram["end"])
                    program["title"] = grabbedProgram["name"]
                except KeyError:
                    # Program with incomplete data (most likely missing 'name').
                    # Cannot create valid XMLTV entry, so skip (data will be updated on a next run when it is available)
                    continue
                
                # Add every program to the internal data structure
                if programId in self._epgdata[channel]:
                    # Existing program, verify it has not been changed
                    stored = self._epgdata[channel][programId]
                    if stored["starttime"] == program["starttime"] and \
                    stored["stoptime"] == program["stoptime"] and \
                    stored["title"] == program["title"]:
                        # Mark stored program as 'grabbed' and skip to next
                        stored["grabbed"] = True
                        continue
                    else:
                        # Changed program, remove from storage and grab new data
                        del self._epgdata[channel][programId]
                
                # New program or program with changes, get details
                if self.details:
                    self._grabDetailedEPG(programId, program)
                
                # Add program to internal storage
                self._epgdata[channel][programId] = program
                
    def _grabDetailedEPG(self, programId, program):
        """Download the detailed program data for the specified program"""
        
        # Generate details URL 
        programIdGroup = programId[-2:]
        detailUrl = "/epgdata/" + programIdGroup + "/" + programId + ".json"
        
        # Try to download file
        try:
            self._epgConnection.request("GET", detailUrl)
            response = self._epgConnection.getresponse()
            if response.status != 200:
                response.read() # Force response buffer to be emptied
                response.close()
                return # No data can be downloaded, return
            
        except (socket.error, http.client.CannotSendRequest, http.client.BadStatusLine):
            # Error in connection. Close existing connection.
            self._epgConnection.close()
            
            # Wait for network to recover
            time.sleep(10)
            
            # Reconnect to server and retry
            try:
                self._epgConnection = http.client.HTTPConnection(self.epgServer)
                self._epgConnection.request("GET", detailUrl)
                response = self._epgConnection.getresponse()
                if response.status != 200:
                    response.read() # Force response buffer to be emptied
                    response.close()
                    return # No data can be downloaded, return
                
            except (socket.error, http.client.CannotSendRequest, http.client.BadStatusLine):
                # Connection remains broken, return (error will be handled in grabEpg function)
                return
        
        detailEpg = json.load(response, "UTF-8")
        response.close()
        
        # Episode title
        if "episodeTitle" in detailEpg and len(detailEpg["episodeTitle"]) > 0:
            program["sub-title"] = detailEpg["episodeTitle"]
            
        # Detailed description
        if "description" in detailEpg and len(detailEpg["description"]) > 0:
            program["desc"] = detailEpg["description"]
            
        # Credits
        program["credits"] = dict()

        if "actors" in detailEpg and len(detailEpg["actors"]) > 0:
            program["credits"]["actor"] = []
            for actor in detailEpg["actors"]:
                program["credits"]["actor"].append(actor)
                
        if "directors" in detailEpg and len(detailEpg["directors"]) > 0:
            program["credits"]["director"] = []
            for director in detailEpg["directors"]:
                program["credits"]["director"].append(director)
                
        if "presenters" in detailEpg and len(detailEpg["presenters"]) > 0:
            program["credits"]["presenter"] = []
            for presenter in detailEpg["presenters"]:
                program["credits"]["presenter"].append(presenter)
                
        if "commentators" in detailEpg and len(detailEpg["commentators"]) > 0:
            program["credits"]["commentator"] = []
            for presenter in detailEpg["commentators"]:
                program["credits"]["commentator"].append(presenter)
                
        # Genres
        if "genres" in detailEpg and len(detailEpg["genres"]) > 0:
            program["categories"] = []
            for genre in detailEpg["genres"]:
                program["categories"].append(genre)
                
        # Aspect ratio
        if "aspectratio" in detailEpg and len(detailEpg["aspectratio"]) > 0:
            program["aspect"] = detailEpg["aspectratio"]
            
        # TODO: NICAM ratings (nicamParentalRating and nicamWarning)
                
    def _getProgramAsElement(self, channel, program):
        """Returns the specified program as an LXML 'Element'"""
        
        # Construct programme tag
        programmeTag = etree.Element("programme",
                                     start      = program["starttime"],
                                     stop       = program["stoptime"],
                                     channel    = channel)
        
        # Construct title tag
        titleTag = etree.Element("title", lang = "nl")
        titleTag.text = program["title"]
        programmeTag.append(titleTag)
        
        # Subtitle
        if "sub-title" in program:
            # Add sub-title tag
            subtitleTag = etree.Element("sub-title", lang = "nl")
            subtitleTag.text = program["sub-title"]
            programmeTag.append(subtitleTag)
            
        # Description
        if "desc" in program:
            # Add desc tag
            descriptionTag = etree.Element("desc", lang = "nl")
            descriptionTag.text = program["desc"]
            programmeTag.append(descriptionTag)
            
        if "rating" in program:
            #add rating tag
            ratingTag = etree.Element("rating", system = "kijkwijzer")
            valueTag = etree.Element("value")
            valueTag.text = program["rating"]
            ratingTag.append(valueTag)
            programmeTag.append(ratingTag)
        
        # Credits (directors, actors, etc)
        if "credits" in program and len(program["credits"]) > 0:
            # Add credits tag
            creditsTag = etree.Element("credits")
            
            # Add tags for each type of credits (in order, so XMLTV stays happy)
            #creditTypes = ["director", "actor", "writer", "adapter",
            #               "producer", "composer", "editor", "presenter",
            #               "commentator", "guest"]
            creditTypes = ["director", "actor", "presenter", "commentator"]
            creditsDict = program["credits"]
            
            for creditType in creditTypes:
                if creditType in creditsDict:
                    for person in creditsDict[creditType]:
                        personTag = etree.Element(creditType)
                        personTag.text = person
                        creditsTag.append(personTag)
                    
            programmeTag.append(creditsTag)
            
        # Categories
        if "categories" in program:
            # Add multiple category tags
            for category in program["categories"]:
                categoryTag = etree.Element("category", lang = "nl")
                categoryTag.text = category
                programmeTag.append(categoryTag)
                
        # Aspect ratio
        if "aspect" in program:
            # Add video tag, containing aspect tag
            videoTag = etree.Element("video")
            aspectTag = etree.Element("aspect")
            aspectTag.text = program["aspect"]
            videoTag.append(aspectTag)
            programmeTag.append(videoTag)
            
        return programmeTag
    
    def _convertTimestamp(self, timestamp):
        """Convert downloaded timestamp to XMLTV compatible time string"""
        startTime = datetime.fromtimestamp(timestamp, self._timezone)
        return startTime.strftime("%Y%m%d%H%M%S %z")
    
    def _getLogo(self, channel):
        """Check if there is a logo for the given channel, and (try) to download it if needed"""
        
        # Check that log has not been verified already
        if channel in self._foundLogos:
            return
        
        # Prepare paths needed for the logo
        if self.logoStore is not None:
            localLogoDir = os.path.join(GRABBERDIR, self.logoStore)
        else:
            localLogoDir = os.path.join(GRABBERDIR, "logos")
        
        logoName = "%s.png" % channel
        localLogo = os.path.join(localLogoDir, logoName)
        remoteLogo = "/tvmenu/images/channels/%s.png" % channel
        
        # Check that logo does not already exist
        if os.path.isfile(localLogo):
            # Found logo, store and return
            self._foundLogos[channel] = localLogo
            return
        
        # Logo not found, try to download it
        try:
            self._epgConnection.request("GET", remoteLogo)
            response = self._epgConnection.getresponse()
            if response.status != 200:
                # Logo cannot be found, set to ignore it
                self._foundLogos[channel] = None
                response.read() # Force response buffer to be emptied
                response.close()
                return
            
        except (socket.error, http.client.CannotSendRequest, http.client.BadStatusLine):
            # Error in connection. Close existing connection.
            self._epgConnection.close()
            
            # Wait for network to recover
            time.sleep(10)
            
            # Reconnect to server and retry
            try:
                self._epgConnection = http.client.HTTPConnection(self.epgServer)
                self._epgConnection.request("GET", remoteLogo)
                response = self._epgConnection.getresponse()
                if response.status != 200:
                    # Logo cannot be found, set to ignore it
                    self._foundLogos[channel] = None
                    response.read() # Force response buffer to be emptied
                    response.close()
                    return
                
            except (socket.error, http.client.CannotSendRequest, http.client.BadStatusLine):
                # Connection remains broken, return (error will be handled in grabEpg function)
                self._foundLogos[channel] = None
                return
        
        # Logo downloaded, store to disk
        try:
            if not os.path.isdir(localLogoDir):
                os.makedirs(localLogoDir)
            
            with open(localLogo, "wb") as logoFile:
                logoFile.write(response.read())
            
            response.close()
            self._foundLogos[channel] = localLogo
        
        except EnvironmentError:
            # Could not store logo, set to ignore it
            self._foundLogos[channel] = None


def main():
    """
    Main entry point of program.
    This function will read the configuration file and start the grabber.
    """
    print("AminoEPGGrabber %s started on %s." % (VERSION, datetime.now()))
    
    # Create grabber class
    grabber = AminoEPGGrabber()
    
    # Try to load config file, if it exists
    configFile = os.path.join(GRABBERDIR, "config.xml")
    if os.path.isfile(configFile):
        grabber.loadConfig(configFile)
    

    # Grab EPG from IPTV network
    if grabber.api == "solocoo":
        grabber.grabEpg_solocoo()
    else:
        # Solocoo doesn't use the database
        grabber.loadDatabase()
        grabber.grabEpg()
        grabber.writeDatabase()
    
    # Write XMLTV file
    grabber.writeXmltv()
    
    print("AminoEPGGrabber finished on %s." % datetime.now())

if __name__ == "__main__":
    main()

