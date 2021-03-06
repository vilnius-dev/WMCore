#!/usr/bin/env python

"""
WorkQueue provides functionality to queue large chunks of work,
thus acting as a buffer for the next steps in job processing

WMSpec objects are fed into the queue, split into coarse grained work units
and released when a suitable resource is found to execute them.

https://twiki.cern.ch/twiki/bin/view/CMS/WMCoreJobPool
"""

from __future__ import division, print_function

import os
import threading
import time
import traceback
from collections import defaultdict

from WMCore import Lexicon
from WMCore.ACDC.DataCollectionService import DataCollectionService
from WMCore.Alerts import API as alertAPI
from WMCore.Database.CMSCouch import CouchInternalServerError, CouchNotFoundError
from WMCore.Services.LogDB.LogDB import LogDB
from WMCore.Services.PhEDEx.PhEDEx import PhEDEx
from WMCore.Services.ReqMgr.ReqMgr import ReqMgr
from WMCore.Services.RequestDB.RequestDBReader import RequestDBReader
from WMCore.Services.SiteDB.SiteDB import SiteDBJSON as SiteDB
from WMCore.Services.WorkQueue.WorkQueue import WorkQueue as WorkQueueDS
from WMCore.WMSpec.WMWorkload import WMWorkloadHelper, getWorkloadFromTask
from WMCore.WorkQueue.DataLocationMapper import WorkQueueDataLocationMapper
from WMCore.WorkQueue.DataStructs.ACDCBlock import ACDCBlock
from WMCore.WorkQueue.DataStructs.WorkQueueElementsSummary import getGlobalSiteStatusSummary
from WMCore.WorkQueue.Policy.End import endPolicy
from WMCore.WorkQueue.Policy.Start import startPolicy
from WMCore.WorkQueue.WorkQueueBackend import WorkQueueBackend
from WMCore.WorkQueue.WorkQueueBase import WorkQueueBase
from WMCore.WorkQueue.WorkQueueExceptions import (TERMINAL_EXCEPTIONS, WorkQueueError, WorkQueueNoMatchingElements,
                                                  WorkQueueWMSpecError)
from WMCore.WorkQueue.WorkQueueUtils import cmsSiteNames, get_dbs


# Convenience constructor functions

def globalQueue(logger=None, dbi=None, **kwargs):
    """Convenience method to create a WorkQueue suitable for use globally
    """
    defaults = {'PopulateFilesets': False,
                'LocalQueueFlag': False,
                'SplittingMapping': {'DatasetBlock':
                                         {'name': 'Block',
                                          'args': {}}
                                    },
                'TrackLocationOrSubscription': 'location'
               }
    defaults.update(kwargs)
    return WorkQueue(logger, dbi, **defaults)


def localQueue(logger=None, dbi=None, **kwargs):
    """Convenience method to create a WorkQueue suitable for use locally
    """
    defaults = {'TrackLocationOrSubscription': 'location'}
    defaults.update(kwargs)
    return WorkQueue(logger, dbi, **defaults)


class WorkQueue(WorkQueueBase):
    """
    _WorkQueue_

    WorkQueue object - interface to WorkQueue functionality.
    """

    def __init__(self, logger=None, dbi=None, **params):

        WorkQueueBase.__init__(self, logger, dbi)
        self.parent_queue = None
        self.params = params

        # config argument (within params) shall be reference to
        # Configuration instance (will later be checked for presence of "Alert")
        self.config = params.get("Config", None)
        self.params.setdefault('CouchUrl', os.environ.get('COUCHURL'))
        if not self.params.get('CouchUrl'):
            raise RuntimeError('CouchUrl config value mandatory')
        self.params.setdefault('DbName', 'workqueue')
        self.params.setdefault('InboxDbName', self.params['DbName'] + '_inbox')
        self.params.setdefault('ParentQueueCouchUrl', None)  # We get work from here

        self.backend = WorkQueueBackend(self.params['CouchUrl'], self.params['DbName'],
                                        self.params['InboxDbName'],
                                        self.params['ParentQueueCouchUrl'], self.params.get('QueueURL'),
                                        logger=self.logger)
        self.workqueueDS = WorkQueueDS(self.params['CouchUrl'], self.params['DbName'],
                                       self.params['InboxDbName'])
        if self.params.get('ParentQueueCouchUrl'):
            try:
                if self.params.get('ParentQueueInboxCouchDBName'):
                    self.parent_queue = WorkQueueBackend(self.params['ParentQueueCouchUrl'].rsplit('/', 1)[0],
                                                         self.params['ParentQueueCouchUrl'].rsplit('/', 1)[1],
                                                         self.params['ParentQueueInboxCouchDBName'])
                else:
                    self.parent_queue = WorkQueueBackend(self.params['ParentQueueCouchUrl'].rsplit('/', 1)[0],
                                                         self.params['ParentQueueCouchUrl'].rsplit('/', 1)[1])
            except IndexError as ex:
                # Probable cause: Someone didn't put the global WorkQueue name in
                # the ParentCouchUrl
                msg = "Parsing failure for ParentQueueCouchUrl - probably missing dbname in input\n"
                msg += "Exception: %s\n" % str(ex)
                msg += str("ParentQueueCouchUrl: %s\n" % self.params['ParentQueueCouchUrl'])
                self.logger.error(msg)
                raise WorkQueueError(msg)
            self.params['ParentQueueCouchUrl'] = self.parent_queue.queueUrl

        self.params.setdefault("GlobalDBS",
                               "https://cmsweb.cern.ch/dbs/prod/global/DBSReader")
        self.params.setdefault('QueueDepth', 1)  # when less than this locally
        self.params.setdefault('WorkPerCycle', 100)
        self.params.setdefault('LocationRefreshInterval', 600)
        self.params.setdefault('FullLocationRefreshInterval', 7200)
        self.params.setdefault('TrackLocationOrSubscription', 'location')
        self.params.setdefault('ReleaseIncompleteBlocks', False)
        self.params.setdefault('ReleaseRequireSubscribed', True)
        self.params.setdefault('PhEDExEndpoint', None)
        self.params.setdefault('PopulateFilesets', True)
        self.params.setdefault('LocalQueueFlag', True)
        self.params.setdefault('QueueRetryTime', 86400)
        self.params.setdefault('stuckElementAlertTime', 172800)
        self.params.setdefault('reqmgrCompleteGraceTime', 604800)
        self.params.setdefault('cancelGraceTime', 86400)

        self.params.setdefault('JobDumpConfig', None)
        self.params.setdefault('BossAirConfig', None)

        self.params['QueueURL'] = self.backend.queueUrl  # url this queue is visible on
        # backend took previous QueueURL and sanitized it
        self.params.setdefault('WMBSUrl', None)  # this will only be set on local Queue
        if self.params.get('WMBSUrl'):
            self.params['WMBSUrl'] = Lexicon.sanitizeURL(self.params['WMBSUrl'])['url']
        self.params.setdefault('Teams', [])

        if self.params.get('CacheDir'):
            try:
                os.makedirs(self.params['CacheDir'])
            except OSError:
                pass
        elif self.params.get('PopulateFilesets'):
            raise RuntimeError('CacheDir mandatory for local queue')

        self.params.setdefault('SplittingMapping', {})
        self.params['SplittingMapping'].setdefault('DatasetBlock',
                                                   {'name': 'Block',
                                                    'args': {}}
                                                  )
        self.params['SplittingMapping'].setdefault('MonteCarlo',
                                                   {'name': 'MonteCarlo',
                                                    'args': {}}
                                                  )
        self.params['SplittingMapping'].setdefault('Dataset',
                                                   {'name': 'Dataset',
                                                    'args': {}}
                                                  )
        self.params['SplittingMapping'].setdefault('Block',
                                                   {'name': 'Block',
                                                    'args': {}}
                                                  )
        self.params['SplittingMapping'].setdefault('ResubmitBlock',
                                                   {'name': 'ResubmitBlock',
                                                    'args': {}}
                                                  )

        self.params.setdefault('EndPolicySettings', {})

        assert (self.params['TrackLocationOrSubscription'] in ('subscription',
                                                               'location'))
        # Can only release blocks on location
        if self.params['TrackLocationOrSubscription'] == 'location':
            if self.params['SplittingMapping']['DatasetBlock']['name'] != 'Block':
                raise RuntimeError('Only blocks can be released on location')

        if self.params.get('PhEDEx'):
            self.phedexService = self.params['PhEDEx']
        else:
            phedexArgs = {}
            if self.params.get('PhEDExEndpoint'):
                phedexArgs['endpoint'] = self.params['PhEDExEndpoint']
            self.phedexService = PhEDEx(phedexArgs)

        if self.params.get('SiteDB'):
            self.SiteDB = self.params['SiteDB']
        else:
            self.SiteDB = SiteDB()

        self.dataLocationMapper = WorkQueueDataLocationMapper(self.logger, self.backend,
                                                              phedex=self.phedexService,
                                                              sitedb=self.SiteDB,
                                                              locationFrom=self.params['TrackLocationOrSubscription'],
                                                              incompleteBlocks=self.params['ReleaseIncompleteBlocks'],
                                                              requireBlocksSubscribed=not self.params[
                                                                  'ReleaseIncompleteBlocks'],
                                                              fullRefreshInterval=self.params[
                                                                  'FullLocationRefreshInterval'],
                                                              updateIntervalCoarseness=self.params[
                                                                  'LocationRefreshInterval'])

        # used for only global WQ
        if self.params.get('ReqMgrServiceURL'):
            self.reqmgrSvc = ReqMgr(self.params['ReqMgrServiceURL'])

        if self.params.get('RequestDBURL'):
            # This is need for getting post call
            # TODO: Change ReqMgr api to accept post for for retrieving the data and remove this
            self.requestDB = RequestDBReader(self.params['RequestDBURL'])

        # initialize alerts sending client (self.sendAlert() method)
        # usage: self.sendAlert(levelNum, msg = msg) ; level - integer 1 .. 10
        #    1 - 4 - lower levels ; 5 - 10 higher levels
        preAlert, self.alertSender = \
            alertAPI.setUpAlertsMessaging(self, compName="WorkQueueManager")
        self.sendAlert = alertAPI.getSendAlert(sender=self.alertSender,
                                               preAlert=preAlert)

        # set the thread name before create the log db.
        # only sets that when it is not set already
        # setLogDB

        myThread = threading.currentThread()
        if myThread.getName() == "MainThread":  # this should be only GQ case other cases thread name should be set
            myThread.setName(self.__class__.__name__)

        centralurl = self.params.get("central_logdb_url")
        identifier = self.params.get("log_reporter")
        self.logdb = LogDB(centralurl, identifier, logger=self.logger)

        self.logger.debug("WorkQueue created successfully")

    def __len__(self):
        """Returns number of Available elements in queue"""
        return self.backend.queueLength()

    def __del__(self):
        """
        Unregister itself with Alert Receiver.
        The registration happened in the constructor when initializing.

        """
        if self.alertSender:
            self.alertSender.unregister()

    def setStatus(self, status, elementIDs=None, SubscriptionId=None, WorkflowName=None):
        """
        _setStatus_, throws an exception if no elements are updated

        """
        try:
            if not elementIDs:
                elementIDs = []
            iter(elementIDs)
            if isinstance(elementIDs, basestring):
                raise TypeError
        except TypeError:
            elementIDs = [elementIDs]

        if status == 'Canceled':  # Cancel needs special actions
            return self.cancelWork(elementIDs, SubscriptionId, WorkflowName)

        args = {}
        if SubscriptionId:
            args['SubscriptionId'] = SubscriptionId
        if WorkflowName:
            args['RequestName'] = WorkflowName

        affected = self.backend.getElements(elementIDs=elementIDs, **args)
        if not affected:
            raise WorkQueueNoMatchingElements("No matching elements")

        for x in affected:
            x['Status'] = status
        elements = self.backend.saveElements(*affected)
        if len(affected) != len(elements):
            raise RuntimeError("Some elements not updated, see log for details")

        return elements

    def setPriority(self, newpriority, *workflowNames):
        """
        Update priority for a workflow, throw exception if no elements affected
        """
        self.logger.info("Priority change request to %s for %s" % (newpriority, str(workflowNames)))
        affected = []
        for wf in workflowNames:
            affected.extend(self.backend.getElements(returnIdOnly=True, RequestName=wf))

        self.backend.updateElements(*affected, Priority=newpriority)

        if not affected:
            raise RuntimeError("Priority not changed: No matching elements")

    def resetWork(self, ids):
        """Put work back in Available state, from here either another queue
         or wmbs can pick it up.

         If work was Acquired by a child queue, the next status update will
         cancel the work in the child.

         Note: That the same child queue is free to pick the work up again,
          there is no permanent blacklist of queues.
        """
        self.logger.info("Resetting elements %s" % str(ids))
        try:
            iter(ids)
        except TypeError:
            ids = [ids]

        return self.backend.updateElements(*ids, Status='Available',
                                           ChildQueueUrl=None, WMBSUrl=None)

    def getWork(self, jobSlots, siteJobCounts, excludeWorkflows=None):
        """
        Get available work from the queue, inject into wmbs & mark as running

        jobSlots is dict format of {site: estimateJobSlot}
        of the resources to get work for.

        siteJobCounts is a dict format of {site: {prio: jobs}}
        """
        excludeWorkflows = excludeWorkflows or []
        results = []
        numElems = self.params['WorkPerCycle']
        if not self.backend.isAvailable():
            self.logger.warning('Backend busy or down: skipping fetching of work')
            return results

        matches, _, _ = self.backend.availableWork(jobSlots, siteJobCounts,
                                                   excludeWorkflows=excludeWorkflows, numElems=numElems)

        if not matches:
            return results

        # cache wmspecs for lifetime of function call, likely we will have multiple elements for same spec.
        # TODO: Check to see if we can skip spec loading - need to persist some more details to element
        wmspecCache = {}
        for match in matches:
            blockName, dbsBlock = None, None
            if self.params['PopulateFilesets']:
                if match['RequestName'] not in wmspecCache:
                    wmspec = self.backend.getWMSpec(match['RequestName'])
                    wmspecCache[match['RequestName']] = wmspec
                else:
                    wmspec = wmspecCache[match['RequestName']]

                try:
                    if match['StartPolicy'] == 'Dataset':
                        # actually returns dataset name and dataset info
                        blockName, dbsBlock = self._getDBSDataset(match)
                    elif match['Inputs']:
                        blockName, dbsBlock = self._getDBSBlock(match, wmspec)
                except Exception as ex:
                    msg = "%s, %s: \n" % (wmspec.name(), match['Inputs'].keys())
                    msg += "failed to retrieve data from DBS/PhEDEx in LQ: \n%s" % str(ex)
                    self.logger.error(msg)
                    self.logdb.post(wmspec.name(), msg, 'error')
                    continue

                try:
                    match['Subscription'] = self._wmbsPreparation(match,
                                                                  wmspec,
                                                                  blockName,
                                                                  dbsBlock)
                    self.logdb.delete(wmspec.name(), "error", this_thread=True)
                except Exception as ex:
                    msg = "%s, %s: \ncreating subscription failed in LQ: \n%s" % (wmspec.name(), blockName, str(ex))
                    self.logger.exception(msg)
                    self.logdb.post(wmspec.name(), msg, 'error')
                    continue

            results.append(match)

        del wmspecCache  # remove cache explicitly
        self.logger.info('Injected %s units into WMBS' % len(results))
        return results

    def _getDBSDataset(self, match):
        """Get DBS info for this dataset"""
        tmpDsetDict = {}
        dbs = get_dbs(match['Dbs'])
        datasetName = match['Inputs'].keys()[0]

        blocks = dbs.listFileBlocks(datasetName, onlyClosedBlocks=True)
        for blockName in blocks:
            tmpDsetDict.update(dbs.getFileBlock(blockName))

        dbsDatasetDict = {'Files': [], 'IsOpen': False, 'PhEDExNodeNames': []}
        dbsDatasetDict['Files'] = [f for block in tmpDsetDict.values() for f in block['Files']]
        dbsDatasetDict['PhEDExNodeNames'].extend(
            [f for block in tmpDsetDict.values() for f in block['PhEDExNodeNames']])
        dbsDatasetDict['PhEDExNodeNames'] = list(set(dbsDatasetDict['PhEDExNodeNames']))

        return datasetName, dbsDatasetDict

    def _getDBSBlock(self, match, wmspec):
        """Get DBS info for this block"""
        blockName = match['Inputs'].keys()[0]  # TODO: Allow more than one

        if match['ACDC']:
            acdcInfo = match['ACDC']
            acdc = DataCollectionService(acdcInfo["server"], acdcInfo["database"])
            splitedBlockName = ACDCBlock.splitBlockName(blockName)
            fileLists = acdc.getChunkFiles(acdcInfo['collection'],
                                           acdcInfo['fileset'],
                                           splitedBlockName['Offset'],
                                           splitedBlockName['NumOfFiles'])

            block = {}
            block["Files"] = fileLists
            return blockName, block
        else:
            dbs = get_dbs(match['Dbs'])
            if wmspec.getTask(match['TaskName']).parentProcessingFlag():
                dbsBlockDict = dbs.getFileBlockWithParents(blockName)
            elif wmspec.requestType() == 'StoreResults':
                dbsBlockDict = dbs.getFileBlock(blockName, dbsOnly=True)
            else:
                dbsBlockDict = dbs.getFileBlock(blockName)

        return blockName, dbsBlockDict[blockName]

    def _wmbsPreparation(self, match, wmspec, blockName, dbsBlock):
        """Inject data into wmbs and create subscription. """
        from WMCore.WorkQueue.WMBSHelper import WMBSHelper
        self.logger.info("Adding WMBS subscription for %s" % match['RequestName'])

        mask = match['Mask']
        wmbsHelper = WMBSHelper(wmspec, match['TaskName'], blockName, mask, self.params['CacheDir'])

        sub, match['NumOfFilesAdded'] = wmbsHelper.createSubscriptionAndAddFiles(block=dbsBlock)
        self.logger.info("Created top level subscription %s for %s with %s files" % (sub['id'],
                                                                                     match['RequestName'],
                                                                                     match['NumOfFilesAdded']))
        # update couch with wmbs subscription info
        match['SubscriptionId'] = sub['id']
        match['Status'] = 'Running'
        # do update rather than save to avoid conflicts from other thread writes
        self.backend.updateElements(match.id, Status='Running', SubscriptionId=sub['id'],
                                    NumOfFilesAdded=match['NumOfFilesAdded'])

        return sub

    def addNewFilesToOpenSubscriptions(self, *elements):
        """Inject new files to wmbs for running elements that have new files.
            Assumes elements are from the same workflow"""
        if not self.params['LocalQueueFlag']:
            return
        wmspec = None
        for ele in elements:
            if not ele.isRunning() or not ele['SubscriptionId'] or not ele:
                continue
            if not ele['Inputs'] or not ele['OpenForNewData'] or ele['StartPolicy'] == 'Dataset':
                continue
            if not wmspec:
                wmspec = self.backend.getWMSpec(ele['RequestName'])
            blockName, dbsBlock = self._getDBSBlock(ele, wmspec)
            if ele['NumOfFilesAdded'] != len(dbsBlock['Files']):
                self.logger.info("Adding new files to open block %s (%s)" % (blockName, ele.id))
                from WMCore.WorkQueue.WMBSHelper import WMBSHelper
                wmbsHelper = WMBSHelper(wmspec, ele['TaskName'], blockName, ele['Mask'], self.params['CacheDir'])
                ele['NumOfFilesAdded'] += wmbsHelper.createSubscriptionAndAddFiles(block=dbsBlock)[1]
                self.backend.updateElements(ele.id, NumOfFilesAdded=ele['NumOfFilesAdded'])
            if dbsBlock['IsOpen'] != ele['OpenForNewData']:
                self.logger.info("Closing open block %s (%s)" % (blockName, ele.id))
                self.backend.updateInboxElements(ele['ParentQueueId'], OpenForNewData=dbsBlock['IsOpen'])
                self.backend.updateElements(ele.id, OpenForNewData=dbsBlock['IsOpen'])
                ele['OpenForNewData'] = dbsBlock['IsOpen']

    def _assignToChildQueue(self, queue, *elements):
        """Assign work from parent to queue"""
        for ele in elements:
            ele['Status'] = 'Negotiating'
            ele['ChildQueueUrl'] = queue
            ele['ParentQueueUrl'] = self.params['ParentQueueCouchUrl']
            ele['WMBSUrl'] = self.params["WMBSUrl"]
        work = self.parent_queue.saveElements(*elements)
        requests = ', '.join(list(set(['"%s"' % x['RequestName'] for x in work])))
        self.logger.info('Acquired work for request(s): %s' % requests)
        return work

    def doneWork(self, elementIDs=None, SubscriptionId=None, WorkflowName=None):
        """Mark work as done
        """
        return self.setStatus('Done', elementIDs=elementIDs,
                              SubscriptionId=SubscriptionId,
                              WorkflowName=WorkflowName)

    def killWMBSWorkflow(self, workflow):
        # import inside function since GQ doesn't need this.
        from WMCore.WorkQueue.WMBSHelper import killWorkflow
        myThread = threading.currentThread()
        myThread.dbi = self.conn.dbi
        myThread.logger = self.logger
        success = True
        try:
            killWorkflow(workflow, self.params["JobDumpConfig"], self.params["BossAirConfig"])
        except Exception as ex:
            success = False
            self.logger.error('Aborting %s wmbs subscription failed: %s' % (workflow, str(ex)))
            self.logger.error('It will be retried in the next loop')
        return success

    def cancelWork(self, elementIDs=None, SubscriptionId=None, WorkflowName=None, elements=None):
        """Cancel work - delete in wmbs, delete from workqueue db, set canceled in inbox
           Elements may be directly provided or determined from series of filter arguments
        """
        if not elements:
            args = {}
            if SubscriptionId:
                args['SubscriptionId'] = SubscriptionId
            if WorkflowName:
                args['RequestName'] = WorkflowName
            elements = self.backend.getElements(elementIDs=elementIDs, **args)

        # take wf from args in case no elements exist for workflow (i.e. work was negotiating)
        requestNames = set([x['RequestName'] for x in elements]) | set([wf for wf in [WorkflowName] if wf])
        if not requestNames:
            return []
        inbox_elements = []
        for wf in requestNames:
            inbox_elements.extend(self.backend.getInboxElements(WorkflowName=wf))

        # if local queue, kill jobs, update parent to Canceled and delete elements
        if self.params['LocalQueueFlag']:
            # if we can talk to wmbs kill the jobs
            badWfsCancel = []
            if self.params['PopulateFilesets']:
                self.logger.info("Canceling work for workflow(s): %s" % (requestNames))
                for workflow in requestNames:
                    if not self.killWMBSWorkflow(workflow):
                        badWfsCancel.append(workflow)
            # now we remove any wf that failed to be cancelled (and its inbox elements)
            requestNames -= set(badWfsCancel)
            for wf in badWfsCancel:
                elementsToRemove = self.backend.getInboxElements(WorkflowName=wf)
                inbox_elements = list(set(inbox_elements) - set(elementsToRemove))
            self.logger.info("New list of cancelled requests: %s" % requestNames)

            # Don't update as fails sometimes due to conflicts (#3856)
            for x in inbox_elements:
                if x['Status'] != 'Canceled':
                    x.load().__setitem__('Status', 'Canceled')

            self.backend.saveElements(*inbox_elements)

        # if global queue, update non-acquired to Canceled, update parent to CancelRequested
        else:
            # Cancel in global if work has not been passed to a child queue
            elements_to_cancel = [x for x in elements if not x['ChildQueueUrl'] and x['Status'] != 'Canceled']
            # ensure all elements receive cancel request, covers case where initial cancel request missed some elements
            # without this elements may avoid the cancel and not be cleared up till they finish
            elements_not_requested = [x for x in elements if
                                      x['ChildQueueUrl'] and (x['Status'] != 'CancelRequested' and not x.inEndState())]

            self.logger.info("""Canceling work for workflow(s): %s""" % (requestNames))
            if elements_to_cancel:
                self.backend.updateElements(*[x.id for x in elements_to_cancel], Status='Canceled')
                self.logger.info("Cancel-ed element(s) %s" % str([x.id for x in elements_to_cancel]))

            if elements_not_requested:
                # Don't update as fails sometimes due to conflicts (#3856)
                for x in elements_not_requested:
                    x.load().__setitem__('Status', 'CancelRequested')
                self.backend.saveElements(*elements_not_requested)
                self.logger.info("CancelRequest-ed element(s) %s" % str([x.id for x in elements_not_requested]))

            self.backend.updateInboxElements(
                *[x.id for x in inbox_elements if x['Status'] != 'CancelRequested' and not x.inEndState()],
                Status='CancelRequested')
            # if we haven't had any updates for a while assume agent is dead and move to canceled
            if self.params.get('cancelGraceTime', -1) > 0 and elements:
                last_update = max([float(x.updatetime) for x in elements])
                if (time.time() - last_update) > self.params['cancelGraceTime']:
                    self.logger.info("%s cancelation has stalled, mark as finished" % elements[0]['RequestName'])
                    # Don't update as fails sometimes due to conflicts (#3856)
                    for x in elements:
                        if not x.inEndState():
                            x.load().__setitem__('Status', 'Canceled')
                    self.backend.saveElements(*[x for x in elements if not x.inEndState()])

        return [x.id for x in elements]

    def deleteWorkflows(self, *requests):
        """Delete requests if finished"""
        for request in requests:
            request = self.backend.getInboxElements(elementIDs=[request])
            if len(request) != 1:
                raise RuntimeError('Invalid number of requests for %s' % request[0]['RequestName'])
            request = request[0]

            if request.inEndState():
                self.logger.info('Deleting request "%s" as it is %s' % (request.id, request['Status']))
                self.backend.deleteElements(request)
            else:
                self.logger.debug('Not deleting "%s" as it is %s' % (request.id, request['Status']))

    def queueWork(self, wmspecUrl, request=None, team=None):
        """
        Take and queue work from a WMSpec.

        If request name is provided but doesn't match WMSpec name
        an error is raised.

        If team is provided work will only be available to queue's
        belonging to that team.

        Duplicate specs will be ignored.
        """
        self.logger.info('queueWork() begin queueing "%s"' % wmspecUrl)
        wmspec = WMWorkloadHelper()
        wmspec.load(wmspecUrl)

        if request:  # validate request name
            if request != wmspec.name():
                raise WorkQueueWMSpecError(wmspec,
                                           'Request & workflow name mismatch %s vs %s' % (request, wmspec.name()))

        # Either pull the existing inbox element or create a new one.
        try:
            inbound = self.backend.getInboxElements(elementIDs=[wmspec.name()], loadSpec=True)
            self.logger.info('Resume splitting of "%s"' % wmspec.name())
        except CouchNotFoundError:
            inbound = [self.backend.createWork(wmspec, Status='Negotiating',
                                               TeamName=team, WMBSUrl=self.params["WMBSUrl"])]
            self.backend.insertElements(inbound)

        work = self.processInboundWork(inbound, throw=True)
        return len(work)

    def addWork(self, requestName):
        """
        Check and add new elements to an existing running request,
        if supported by the start policy.
        """
        self.logger.info('addWork() checking "%s"' % requestName)
        inbound = None
        try:
            inbound = self.backend.getInboxElements(elementIDs=[requestName], loadSpec=True)
        except CouchNotFoundError:
            # This shouldn't happen, the request is in running-open therefore it must exist in the inbox
            self.logger.error('Can not find request %s for work addition' % requestName)
            return 0

        work = []
        if inbound:
            work = self.processInboundWork(inbound, throw=True, continuous=True)
        return len(work)

    def status(self, status=None, elementIDs=None,
               dictKey=None, syncWithWMBS=False, loadSpec=False,
               **filters):
        """
        Return elements in the queue.

        status, elementIDs & filters are 'AND'ed together to filter elements.
        dictKey returns the output as a dict with the dictKey as the key.
        syncWithWMBS causes elements to be synced with their status in WMBS.
        loadSpec causes the workflow for each spec to be loaded.
        """
        items = self.backend.getElements(status=status,
                                         elementIDs=elementIDs,
                                         loadSpec=loadSpec,
                                         **filters)

        if syncWithWMBS:
            from WMCore.WorkQueue.WMBSHelper import wmbsSubscriptionStatus
            wmbs_status = wmbsSubscriptionStatus(logger=self.logger,
                                                 dbi=self.conn.dbi,
                                                 conn=self.conn.getDBConn(),
                                                 transaction=self.conn.existingTransaction())
            for item in items:
                for wmbs in wmbs_status:
                    if item['SubscriptionId'] == wmbs['subscription_id']:
                        item.updateFromSubscription(wmbs)
                        break

        # if dictKey, format as a dict with the appropriate key
        if dictKey:
            tmp = defaultdict(list)
            for item in items:
                tmp[item[dictKey]].append(item)
            items = dict(tmp)
        return items

    def statusInbox(self, status=None, elementIDs=None, dictKey=None, **filters):
        """
        Return elements in the inbox.

        status, elementIDs & filters are 'AND'ed together to filter elements.
        dictKey returns the output as a dict with the dictKey as the key.
        """
        items = self.backend.getInboxElements(status, elementIDs, **filters)

        # if dictKey, given format as a dict with the appropriate key
        if dictKey:
            tmp = defaultdict(list)
            for item in items:
                tmp[item[dictKey]].append(item)
            items = dict(tmp)

        return items

    def updateLocationInfo(self):
        """
        Update locations info for elements.
        """
        if not self.backend.isAvailable():
            self.logger.info('Backend busy or down: skipping location update')
            return 0
        result = self.dataLocationMapper()
        self.backend.recordTaskActivity('location_refresh')
        return result

    def _printLog(self, msg, printFlag, logLevel):
        if printFlag:
            print(msg)
        else:
            getattr(self.logger, logLevel)(msg)

    def pullWorkConditionCheck(self, printFlag=False):

        if not self.params['ParentQueueCouchUrl']:
            msg = 'Unable to pull work from parent, ParentQueueCouchUrl not provided'
            self._printLog(msg, printFlag, "warning")
            return False
        if not self.backend.isAvailable() or not self.parent_queue.isAvailable():
            msg = 'Backend busy or down: skipping work pull'
            self._printLog(msg, printFlag, "warning")
            return False

        left_over = self.parent_queue.getElements('Negotiating', returnIdOnly=True,
                                                  ChildQueueUrl=self.params['QueueURL'])
        if left_over:
            msg = 'Not pulling more work. Still replicating %d previous units, ids:\n%s' % (len(left_over), left_over)
            self._printLog(msg, printFlag, "warning")
            return False

        still_processing = self.backend.getInboxElements('Negotiating', returnIdOnly=True)
        if still_processing:
            msg = 'Not pulling more work. Still processing %d previous units' % len(still_processing)
            self._printLog(msg, printFlag, "warning")
            return False

        return True

    def freeResouceCheck(self, resources=None, printFlag=False):

        jobCounts = {}
        if not resources:
            # find out available resources from wmbs
            from WMCore.WorkQueue.WMBSHelper import freeSlots
            thresholds, jobCounts = freeSlots(self.params['QueueDepth'], knownCmsSites=cmsSiteNames())
            # resources for new work are free wmbs resources minus what we already have queued
            _, resources, jobCounts = self.backend.availableWork(thresholds, jobCounts)

        if not resources:
            msg = 'Not pulling more work. No free slots.'
            self._printLog(msg, printFlag, "warning")
            return (False, False)

        return (resources, jobCounts)

    def getAvailableWorkfromParent(self, resources, jobCounts, printFlag=False):
        numElems = self.params['WorkPerCycle']
        work, _, _ = self.parent_queue.availableWork(resources, jobCounts, self.params['Teams'], numElems=numElems)

        if not work:
            msg = 'No available work in parent queue.'
            self._printLog(msg, printFlag, "warning")
        return work

    def pullWork(self, resources=None):
        """
        Pull work from another WorkQueue to be processed

        If resources passed in get work for them, if not available resources
        from get from wmbs.
        """
        if self.pullWorkConditionCheck() == False:
            return 0

        (resources, jobCounts) = self.freeResouceCheck(resources)
        if (resources, jobCounts) == (False, False):
            return 0

        self.logger.info("Pull work for sites %s: " % str(resources))
        work = self.getAvailableWorkfromParent(resources, jobCounts)
        if not work:
            return 0

        work = self._assignToChildQueue(self.params['QueueURL'], *work)

        return len(work)

    def closeWork(self, *workflows):
        """
        Global queue service that looks for the inbox elements that are still running open
        and checks whether they should be closed already. If a list of workflows
        is specified then those workflows are closed regardless of their current status.
        An element is closed automatically when one of the following conditions holds true:
        - The StartPolicy doesn't define a OpenRunningTimeout or this delay is set to 0
        - A period longer than OpenRunningTimeout has passed since the last child element was created or an open block was found
          and the StartPolicy newDataAvailable function returns False.
        It also checks if new data is available and updates the inbox element
        """

        if not self.backend.isAvailable():
            self.logger.warning('Backend busy or down: Can not close work at this time')
            return

        if self.params['LocalQueueFlag']:
            return  # GlobalQueue-only service

        if workflows:
            workflowsToClose = workflows
        else:
            workflowsToCheck = self.backend.getInboxElements(OpenForNewData=True)
            workflowsToClose = []
            currentTime = time.time()
            for element in workflowsToCheck:
                # Easy check, close elements with no defined OpenRunningTimeout
                policy = element.get('StartPolicy', {})
                openRunningTimeout = policy.get('OpenRunningTimeout', 0)
                if not openRunningTimeout:
                    # Closing, no valid OpenRunningTimeout available
                    workflowsToClose.append(element.id)
                    continue

                # Check if new data is currently available
                skipElement = False
                spec = self.backend.getWMSpec(element.id)
                for topLevelTask in spec.taskIterator():
                    policyName = spec.startPolicy()
                    if not policyName:
                        raise RuntimeError("WMSpec doesn't define policyName, current value: '%s'" % policyName)

                    policyInstance = startPolicy(policyName, self.params['SplittingMapping'])
                    if not policyInstance.supportsWorkAddition():
                        continue
                    if policyInstance.newDataAvailable(topLevelTask, element):
                        skipElement = True
                        self.backend.updateInboxElements(element.id, TimestampFoundNewData=currentTime)
                        msg = "There are blocks still open for writing in DBS."
                        self.logdb.post(element['RequestName'], msg, "warning")
                        break
                if skipElement:
                    continue

                # Check if the delay has passed
                newDataFoundTime = element.get('TimestampFoundNewData', 0)
                childrenElements = self.backend.getElementsForParent(element)
                if len(childrenElements) > 0:
                    lastUpdate = float(max(childrenElements, key=lambda x: x.timestamp).timestamp)
                    if (currentTime - max(newDataFoundTime, lastUpdate)) > openRunningTimeout:
                        workflowsToClose.append(element.id)
                    # if it is successful remove previous error
                    self.logdb.delete(element.id, "error", this_thread=True)
                else:
                    msg = "ChildElement is empty for element id %s: investigate" % element.id
                    self.logdb.post(element.id, msg, "error")
                    # self.logdb.upload2central(element.id)
                    self.logger.error(msg)

        msg = 'No workflows to close.\n'
        if workflowsToClose:
            try:
                self.backend.updateInboxElements(*workflowsToClose, OpenForNewData=False)
                msg = 'Closed workflows : %s.\n' % ', '.join(workflows)
            except CouchInternalServerError as ex:
                msg = 'Failed to close workflows. Error was CouchInternalServerError.'
                self.logger.error(msg)
                self.logger.error('Error message: %s' % str(ex))
                raise
            except Exception as ex:
                msg = 'Failed to close workflows. Generic exception caught.'
                self.logger.error(msg)
                self.logger.error('Error message: %s' % str(ex))

        self.backend.recordTaskActivity('workclosing', msg)

        return workflowsToClose

    def deleteCompletedWFElements(self):
        """
        deletes Workflow when workflow is in finished status
        """
        deletableStates = ["completed", "closed-out", "failed",
                           "announced", "aborted-completed", "rejected",
                           "normal-archived", "aborted-archived", "rejected-archived"]

        reqNames = self.backend.getWorkflows(includeInbox=True, includeSpecs=True)
        requestsInfo = self.requestDB.getRequestByNames(reqNames)
        deleteRequests = []
        for key, value in requestsInfo.items():
            if (value["RequestStatus"] == None) or (value["RequestStatus"] in deletableStates):
                deleteRequests.append(key)

        return self.backend.deleteWQElementsByWorkflow(deleteRequests)

    def performSyncAndCancelAction(self, skipWMBS):
        """
        Apply end policies to determine work status & cleanup finished work
        """
        if not self.backend.isAvailable():
            self.logger.warning('Backend busy or down: skipping cleanup tasks')
            return

        if self.params['LocalQueueFlag']:
            self.backend.fixConflicts()  # before doing anything fix any conflicts

        wf_to_cancel = []  # record what we did for task_activity
        finished_elements = []

        useWMBS = not skipWMBS and self.params['LocalQueueFlag']
        # Get queue elements grouped by their workflow with updated wmbs progress
        # Cancel if requested, update locally and remove obsolete elements
        for wf in self.backend.getWorkflows(includeInbox=True, includeSpecs=True):
            try:
                elements = self.status(RequestName=wf, syncWithWMBS=useWMBS)
                parents = self.backend.getInboxElements(RequestName=wf)

                self.logger.debug("Queue status follows:")
                results = endPolicy(elements, parents, self.params['EndPolicySettings'])
                for result in results:
                    self.logger.debug(
                        "Request %s, Status %s, Full info: %s" % (result['RequestName'], result['Status'], result))

                    # check for cancellation requests (affects entire workflow)
                    if result['Status'] == 'CancelRequested':
                        canceled = self.cancelWork(WorkflowName=wf)
                        if canceled:  # global wont cancel if work in child queue
                            wf_to_cancel.append(wf)
                            break
                    elif result['Status'] == 'Negotiating':
                        self.logger.debug("Waiting for %s to finish splitting" % wf)
                        continue

                    parent = result['ParentQueueElement']
                    if parent.modified:
                        self.backend.saveElements(parent)

                    if result.inEndState():
                        if elements:
                            self.logger.info(
                                "Request %s finished (%s)" % (result['RequestName'], parent.statusMetrics()))
                            finished_elements.extend(result['Elements'])
                        else:
                            self.logger.info('Waiting for parent queue to delete "%s"' % result['RequestName'])
                        continue

                    self.addNewFilesToOpenSubscriptions(*elements)

                    updated_elements = [x for x in result['Elements'] if x.modified]
                    for x in updated_elements:
                        self.logger.debug("Updating progress %s (%s): %s" % (x['RequestName'], x.id, x.statusMetrics()))
                    if not updated_elements and (
                        float(parent.updatetime) + self.params['stuckElementAlertTime']) < time.time():
                        self.sendAlert(5, msg='Element for %s stuck for 24 hours.' % wf)
                    for x in updated_elements:
                        self.backend.updateElements(x.id, **x.statusMetrics())
            except Exception as ex:
                self.logger.error('Error processing workflow "%s": %s' % (wf, str(ex)))

        msg = 'Finished elements: %s\nCanceled workflows: %s' % (', '.join(["%s (%s)" % (x.id, x['RequestName']) \
                                                                            for x in finished_elements]),
                                                                 ', '.join(wf_to_cancel))
        self.backend.recordTaskActivity('housekeeping', msg)

    def performQueueCleanupActions(self, skipWMBS=False):

        try:
            self.deleteCompletedWFElements()
        except Exception as ex:
            msg = traceback.format_exc()
            self.logger.error('Error deleting wq elements  "%s": %s' % (str(ex), msg))

        try:
            self.performSyncAndCancelAction(skipWMBS)
        except Exception as ex:
            msg = traceback.format_exc()
            self.logger.error('Error canceling wq elements  "%s": %s' % (str(ex), msg))

    def _splitWork(self, wmspec, data=None, mask=None, inbound=None, continuous=False):
        """
        Split work from a parent into WorkQeueueElements.

        If data param supplied use that rather than getting input data from
        wmspec. Used for instance when global splits by Block (avoids having to
        modify wmspec block whitelist - thus all appear as same wf in wmbs)

        mask can be used to specify i.e. event range.

        The inbound and continous parameters are used to split
        and already split inbox element.
        """
        totalUnits = []
        # split each top level task into constituent work elements
        # get the acdc server and db name
        for topLevelTask in wmspec.taskIterator():
            spec = getWorkloadFromTask(topLevelTask)
            policyName = spec.startPolicy()
            if not policyName:
                raise RuntimeError("WMSpec doesn't define policyName, current value: '%s'" % policyName)

            policy = startPolicy(policyName, self.params['SplittingMapping'])
            if not policy.supportsWorkAddition() and continuous:
                # Can't split further with a policy that doesn't allow it
                continue
            if continuous:
                policy.modifyPolicyForWorkAddition(inbound)
            self.logger.info('Splitting %s with policy %s params = %s' % (topLevelTask.getPathName(),
                                                                          policyName, self.params['SplittingMapping']))
            units, rejectedWork = policy(spec, topLevelTask, data, mask, continuous=continuous)
            for unit in units:
                msg = 'Queuing element %s for %s with %d job(s) split with %s' % (unit.id,
                                                                                  unit['Task'].getPathName(),
                                                                                  unit['Jobs'], policyName)
                if unit['Inputs']:
                    msg += ' on %s' % unit['Inputs'].keys()[0]
                if unit['Mask']:
                    msg += ' on events %d-%d' % (unit['Mask']['FirstEvent'], unit['Mask']['LastEvent'])
                self.logger.info(msg)
            totalUnits.extend(units)

        return (totalUnits, rejectedWork)

    def _getTotalStats(self, units):
        totalToplevelJobs = 0
        totalEvents = 0
        totalLumis = 0
        totalFiles = 0

        for unit in units:
            totalToplevelJobs += unit['Jobs']
            totalEvents += unit['NumberOfEvents']
            totalLumis += unit['NumberOfLumis']
            totalFiles += unit['NumberOfFiles']

        return {'total_jobs': totalToplevelJobs,
                'input_events': totalEvents,
                'input_lumis': totalLumis,
                'input_num_files': totalFiles}

    def processInboundWork(self, inbound_work=None, throw=False, continuous=False):
        """Retrieve work from inbox, split and store
        If request passed then only process that request
        """
        if self.params['LocalQueueFlag']:
            self.logger.info("fixing conflict...")
            self.backend.fixConflicts()  # db should be consistent

        result = []
        if not inbound_work and continuous:
            # This is not supported
            return result
        if not inbound_work:
            inbound_work = self.backend.getElementsForSplitting()
        for inbound in inbound_work:
            try:
                # Check we haven't already split the work, unless it's continuous processing
                work = not continuous and self.backend.getElementsForParent(inbound)
                if work:
                    self.logger.info('Request "%s" already split - Resuming' % inbound['RequestName'])
                else:
                    work, rejectedWork = self._splitWork(inbound['WMSpec'], data=inbound['Inputs'],
                                                         mask=inbound['Mask'], inbound=inbound,
                                                         continuous=continuous)

                    # save inbound work to signal we have completed queueing
                    # if this fails, rerunning will pick up here
                    newWork = self.backend.insertElements(work, parent=inbound)
                    # get statistics for the new work
                    totalStats = self._getTotalStats(newWork)

                    if not continuous:
                        # Update to Acquired when it's the first processing of inbound work
                        self.backend.updateInboxElements(inbound.id, Status='Acquired')

                    # store the inputs in the global queue inbox workflow element
                    if not self.params.get('LocalQueueFlag'):
                        processedInputs = []
                        for unit in work:
                            processedInputs.extend(unit['Inputs'].keys())
                        self.backend.updateInboxElements(inbound.id, ProcessedInputs=processedInputs,
                                                                 RejectedInputs=rejectedWork)
                        # if global queue, then update workflow stats to request mgr couch doc
                        # remove the "UnittestFlag" - need to create the reqmgrSvc emulator
                        if not self.params.get("UnittestFlag", False):
                            self.reqmgrSvc.updateRequestStats(inbound['WMSpec'].name(), totalStats)

            except TERMINAL_EXCEPTIONS as ex:
                msg = 'Terminal exception splitting WQE: %s' % inbound
                self.logger.error(msg)
                self.logdb.post(inbound['RequestName'], msg, 'error')
                if not continuous:
                    # Only fail on first splitting
                    self.logger.error('Failing workflow "%s": %s' % (inbound['RequestName'], str(ex)))
                    self.backend.updateInboxElements(inbound.id, Status='Failed')
                    if throw:
                        raise
            except Exception as ex:
                if continuous:
                    continue
                msg = 'Exception splitting wqe %s for %s: %s' % (inbound.id, inbound['RequestName'], str(ex))
                self.logger.error(msg)
                self.logdb.post(inbound['RequestName'], msg, 'error')

                if throw:
                    raise
                continue
            else:
                result.extend(work)

        requests = ', '.join(list(set(['"%s"' % x['RequestName'] for x in result])))
        if requests:
            self.logger.info('Split work for request(s): %s' % requests)

        return result

    def getWMBSInjectionStatus(self, workflowName=None, drainMode=False):
        """
        if the parent queue exist return the result from parent queue.
        other wise return the result from the current queue.
        (In general parent queue always exist when it is called from local queue
        except T1 skim case)
        returns list of [{workflowName: injection status (True or False)}]
        if the workflow is not exist return []
        """
        if self.parent_queue and not drainMode:
            return self.parent_queue.getWMBSInjectStatus(workflowName)
        else:
            return self.backend.getWMBSInjectStatus(workflowName)

    def monitorWorkQueue(self, status=None):
        """
        Uses the workqueue data-service to retrieve a few basic information
        regarding all the elements in the queue.
        """
        status = status or []
        results = {}
        start = int(time.time())
        results['workByStatus'] = self.workqueueDS.getJobsByStatus()
        results['workByStatusAndPriority'] = self.workqueueDS.getJobsByStatusAndPriority()
        results['workByAgentAndStatus'] = self.workqueueDS.getChildQueuesAndStatus()
        results['workByAgentAndPriority'] = self.workqueueDS.getChildQueuesAndPriority()

        # now the heavy procesing for the site information
        elements = self.workqueueDS.getElementsByStatus(status)
        uniSites, posSites = getGlobalSiteStatusSummary(elements)
        results['uniqueJobsPerSiteAAA'] = uniSites
        results['possibleJobsPerSiteAAA'] = posSites
        uniSites, posSites = getGlobalSiteStatusSummary(elements, dataLocality=True)
        results['uniqueJobsPerSite'] = uniSites
        results['possibleJobsPerSite'] = posSites

        end = int(time.time())
        results["total_query_time"] = end - start
        return results
