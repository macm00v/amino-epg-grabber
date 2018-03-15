'''
Created on Mar 14, 2018

@author: jeweet
'''

from urllib.error import URLError
import datetime
import hashlib
import os
import sqlite3
import urllib.request
from posix import O_RDONLY

class fileCache(object):
    '''
    A cached file store to avoid duplicate downloads/files
    '''
    
    def __init__(self, folder, director, retention=1):
        '''
        Create a new fileCache object.
        folder: the store directory of the object
        director: an urllib.request.OpenerDirector instance to use
        retention: amount of days to keep references/files after expiration  
        '''
        self.director = director
        self.retention = retention
        self._db = None
        self._dbfile = os.path.join(folder, "fileCache.sqlite")
        self._store = folder
        self._storefd = None
        
        if os.path.isdir(folder):
            if not os.path.isfile(self._dbfile) and len(os.listdir(folder)) != 0:
                # Non-empty non-store, bail!
                return False
        else:
            os.makedirs(folder)
        
        if os.supports_dir_fd:
            self._storefd = os.open(folder, O_RDONLY) 
        self._db = sqlite3.connect(self._dbfile)
        self._initDb()
        
    def cleanUp(self):
        '''
        Clean up all non-referenced files and their related database entries.
        '''
        curs = self._db.cursor()
        keepafter = datetime.datetime.now() - \
                    datetime.timedelta(days=self.retention)
        curs.execute("DELETE FROM idx " + \
                     "WHERE expires < ?", [keepafter])
        
        curs.execute("SELECT fileID, path FROM files " + \
                     "WHERE NOT EXISTS (" + \
                        "SELECT fileID FROM idx " + \
                        "WHERE idx.fileID IS files.fileID" + \
                     ")")

        expired = curs.fetchall()        
        if len(expired) > 0:
            for row in expired:
                os.remove(row[1], dir_fd=self._storefd)
            curs.execute("DELETE FROM files " + \
                     "WHERE NOT EXISTS (" + \
                        "SELECT fileID FROM idx " + \
                        "WHERE idx.fileID IS files.fileID" + \
                     ")")
        self._db.commit()
        curs.close()
        
        
    def getFilePath(self, reference, expires=datetime.datetime.now(), 
                    url=None):
        '''
        Returns path to file by reference and url (optional).
        reference: string to identify reference entry
        expires: datetime object with expiry date and time
        url: optional, url to original source
        If reference does not exist and url is given, file will be downloaded
        and reference will be created.
        Returns false on failure or on cache miss if url is not given.
        '''
        curs = self._db.cursor()
        curs.execute("SELECT idx.ref, files.path, files.url FROM idx " + \
                     "JOIN files ON idx.fileID IS files.fileID " + \
                     "WHERE idx.ref IS ?", [reference])
        row = curs.fetchone()
        if row:
            # We have a reference, return the full path is the url is unchanged
            curs.close()
            if not url or row[2] == url:
                return os.path.join(self._store, row[1])
        else:
            # No reference exists, check if we have the requested file
            if not url:
                curs.close()
                return False
            
            curs.execute("SELECT fileID, url, path FROM files " + \
                         "WHERE url IS ?", [url])
            row = curs.fetchone()
            curs.close()
            if row:
                # We have the file! Just add a reference
                self._addRef(reference, expires, row[0])
                return os.path.join(self._store, row[2])
                
        fname = self._update(reference, expires, url)
        if fname:
            fname = os.path.join(self._store, fname)
        return fname
        
    
    ###
    # Private stuff
    ###
    def _initDb(self):
        curs = self._db.cursor()
        curs.execute("CREATE TABLE IF NOT EXISTS 'files' (" + \
                     "'fileID' INTEGER PRIMARY KEY AUTOINCREMENT, " + \
                     "'url' TEXT UNIQUE NOT NULL, " + \
                     "'path' TEXT UNIQUE NOT NULL"
                     ")")
        curs.execute("CREATE TABLE IF NOT EXISTS 'idx' (" + \
                     "'refID' INTEGER PRIMARY KEY AUTOINCREMENT, " + \
                     "'ref' TEXT UNIQUE NOT NULL, " + \
                     "'expires' INTEGER, "
                     "'fileID' INTEGER REFERENCES files('fileID') " + \
                     "ON DELETE CASCADE)")
        self._db.commit()
        curs.close()
        return True
    
    
    def _addRef(self, reference, expires, fileID):
        '''
        Add a reference to a file.
        Returns path to file or False on failure.
        '''
        curs = self._db.cursor()
        curs.execute("INSERT OR REPLACE INTO idx (ref, expires, fileID) " + \
                     "VALUES (?, ?, ?)", [reference, expires, fileID])
        self._db.commit()
        curs.close()
    
    
    def _update(self, reference, expires, url):
        '''
        Update reference with url, overwriting existing data where necessary.
        Returns path to file or False on failure.
        '''
        try:
            req = urllib.request.Request(url)
            resp = self.director.open(req)
        except URLError:
            # Fail silently, return false
            return False
        
        path = hashlib.sha1(url.encode("utf-8")).hexdigest() + os.path.splitext(url)[1]
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_CREAT, dir_fd=self._storefd)
        os.write(fd, resp.read())
        os.close(fd)
        
        curs = self._db.cursor()
        curs.execute("INSERT OR REPLACE INTO files (url, path) " + \
                     "VALUES (?, ?)", [url, path])
        curs.execute("INSERT OR REPLACE " + \
                     "INTO idx (ref, expires, fileID) " + \
                     "VALUES (?, ?, ?)", [reference, expires, curs.lastrowid])
        self._db.commit()
        curs.close()
        
        return path
    
    
    
    
    