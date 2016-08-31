#!/usr/bin/env python

"""
Auto generated stub be careful with editing,
Inheritance is preferred.
"""
from __future__ import absolute_import
import threading
import logging
# harness class that encapsulates the basic component logic.
from WMCore.Agent.Harness import Harness
from .WMAgentTasks import WMAgentTasks
from WMQuality.Emulators.PhEDExClient.MockPhEDExApi import SITES as DUMMY_SITES

class WMAgentEmulator(Harness):


    def __init__(self, config):
        # call the base class
        Harness.__init__(self, config)


    def preInitialization(self):
        resources = self.populateResources()
        # Add event loop to worker manager
        myThread = threading.currentThread()
        pollInterval = 1
        logging.info("Setting poll interval to %s seconds" % pollInterval)
        myThread.workerThreadManager.addWorker(WMAgentTasks(resources), pollInterval)

        return

    def populateResources(self):
        """
        emulating resource db which can represent
        {site: job} format
        """
        jobSites = {}
        for site in DUMMY_SITES:
            jobSites[site] = 100
        return jobSites
