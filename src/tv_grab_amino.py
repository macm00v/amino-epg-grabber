#!/usr/bin/env python

"""
A XMLTV compatible EPG grabber for the Amino EPG.

The grabber should function for any provider that supplies IPTV from Glashart Media.
"""
from lib.AminoEPGGrabber import AminoEPGGrabber
from datetime import datetime
import inspect
import os   

GRABBERDIR = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))

def main():
    """
    Main entry point of program.
    This function will read the configuration file and start the grabber.
    """
    # Create grabber object
    grabber = AminoEPGGrabber(GRABBERDIR)

    # Grab EPG from IPTV network
    if grabber.api == "solocoo":
        # Solocoo doesn't use the database
        grabber.grabEpg_solocoo()
    else:
        grabber.loadDatabase()
        grabber.grabEpg()
        grabber.writeDatabase()
    
    # Write XMLTV file
    grabber.writeXmltv()
    
    print("AminoEPGGrabber finished on %s." % datetime.now())

if __name__ == "__main__":
    main()

