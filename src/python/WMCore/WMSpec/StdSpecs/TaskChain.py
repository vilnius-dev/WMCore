#!/usr/bin/env python
# encoding: utf-8
"""
_TaskChain_

Created by Dave Evans on 2011-06-15.
Copyright (c) 2011 Fermilab. All rights reserved.

Provide support for building arbitrary chains of WMTasks based on a nested dictionary structure
starting with either a generation (create new MC events) or processing (use an existing input dataset) step, followed
by a chain of dependent WMTasks that process the subsequent output.

The request is formed as a dictionary where some global parameters are provided as normal, but the
processing tasks are specified as sub dictionaries.

The top level dict should contain the parameter TaskChain and the value is the number of processing tasks to be run.
For each count in the chain, a dictionary entry named Task1...N should be made with a value being another dictionary.

Any parameters in the Main request will be used throughout the different task unless they are overriden, exceptions are
CouchDB parameters the main request parameters are:

{
    "CMSSWVersion": "CMSSW_3_5_8",                    CMSSW Version
    "ScramArch": "slc5_ia32_gcc434",                  Scram Arch
    "Requestor": "sfoulkes@fnal.gov",                 Person responsible
    "GlobalTag": "GR10_P_v4::All",                    Global Tag
    "ConfigCacheUrl": https://cmsweb-testbed.cern.ch/couchdb URL of CouchDB containing ConfigCache docs for all tasks
    "CouchDBName": "config_cache",                    Name of Couch Database containing config cache (Used for all sub-tasks)
    "TaskChain" : 4,                                  Define number of tasks in chain.
}


Task1 will be either a generation or processing task:

Example initial generation task:

"Task1" :{
    "TaskName"           : "GenSim",                 Task Name
    "ConfigCacheID"      : generatorDoc,             Generator Config id
    "SplittingAlgorithm" : "EventBased",             Splitting Algorithm
    "SplittingArguments" : {"events_per_job" : 250}, Size of jobs in terms of splitting algorithm
    "RequestNumEvents"   : 10000,                    Total number of events to generate
    "Seeding"            : "AutomaticSeeding",       Random seeding method
    "PrimaryDataset"     : "RelValTTBar",            Primary Dataset to be created
    "ScramArch"          : "slc5_amd64_gcc462",      Particular scramArch for this task
    "CMSSWVersion"       : "CMSSW_5_3_5",            Particular CMSSW version for this task
},

Example initial processing task

"Task1" :{
     "TaskName"           : "DigiHLT",                                 Task Name
     "ConfigCacheID"      : someHash,                                  Processing Config id
     "InputDataset"       : "/MinimumBias/Commissioning10-v4/GEN-SIM", Input Dataset to be processed
     "SplittingAlgorithm" : "FileBased",                               Splitting Algorithm
     "SplittingArguments" : {"files_per_job" : 1},                     Size of jobs in terms of splitting algorithm
     "MCPileup"           : "/MinBias/Summer12-v1/GEN-SIM-DIGI-RECO",  Pileup MC dataset for the task
     "DataPileup"         : "/MinimumBias/Run2011A-v1/RAW"             Pileup data dataset for the task
     "GlobalTag"          : "GR_P_V42::All"                            Global tag for  this task
     "KeepOutput"         : False                                      Indicates if the output data from this dataset should be kept in merged area
 },

 All subsequent Task entries will process the output of one of the preceeding steps, the primary dataset can be changed from the input.

 Example:

 "Task2" : {
     "TaskName"              : "Reco",                        Task Name
     "InputTask"             : "DigiHLT",                     Input Task Name (Task Name field of a previous Task entry)
     "InputFromOutputModule" : "writeRAWDIGI",                OutputModule name in the input task that will provide files to process
     "ConfigCacheID"         : "17612875182763812763812",     Processing Config id
     "SplittingAlgorithm"    : "FileBased",                   Splitting Algorithm
     "SplittingArguments"    : {"files_per_job" : 1 },        Size of jobs in terms of splitting algorithm
     "DataPileup"            : "/MinimumBias/Run2011A-v1/RAW" Pileup data dataset for the task
 },

 "Task3" : {
     "TaskName"              : "ALCAReco",             Task Name
     "InputTask"             : "Reco",                 Input Task Name (Task Name field of a previous Task entry)
     "InputFromOutputModule" : "writeALCA",            OutputModule name in the input task that will provide files to process
     "ConfigCacheID"         : "12871372323918187281", Processing Config id
     "SplittingAlgorithm"    : "FileBased",            Splitting Algorithm
     "SplittingArguments"    : {"files_per_job" : 1 }, Size of jobs in terms of splitting algorithm
 },
"""
from __future__ import division
from Utils.Utilities import makeList, strToBool
from WMCore.Lexicon import primdataset
from WMCore.WMSpec.StdSpecs.StdBase import StdBase
from WMCore.WMSpec.WMWorkloadTools import validateArgumentsCreate, parsePileupConfig

#
# simple utils for data mining the request dictionary
#
isGenerator = lambda args: not args["Task1"].get("InputDataset", None)
parentTaskModule = lambda args: args.get("InputFromOutputModule", None)


class ParameterStorage(object):
    """
    _ParameterStorage_

    Decorator class which storages global parameters,
    sets them to local values before executing the passed function
    and restores them afterwards. This is only suited to decorate the
    setupTask and setupGeneratorTask in TaskChainWorkloadFactory.
    """

    def __init__(self, func):
        """
        __init__

        Stores the function and valid parameters to save/restore.

        The validParameters dictionary contains a mapping from the name of the attribute
        in StdBase to the argument key in the task dictionaries
        """
        self.func = func
        self.validParameters = {'globalTag': 'GlobalTag',
                                'frameworkVersion': 'CMSSWVersion',
                                'scramArch': 'ScramArch',
                                'processingVersion': 'ProcessingVersion',
                                'processingString': 'ProcessingString',
                                'acquisitionEra': 'AcquisitionEra',
                                'timePerEvent': 'TimePerEvent',
                                'sizePerEvent': 'SizePerEvent',
                                'memory': 'Memory'
                               }
        return

    def __get__(self, instance, owner):
        """
        __get__

        Get method for the class, store the calling instance for latter use
        """
        self.obj = instance
        self.cls = owner
        return self.__call__

    def __call__(self, task, taskConf):
        """
        __call__

        Store the global parameters, alters the parameters
        using the taskConf argument. Executes the stored
        method, then restores the parameters and resets the local instance.
        """
        self.storeParameters()
        self.alterParameters(taskConf)
        self.func(self.obj, task, taskConf)
        self.restoreParameters()
        self.resetParameters()
        return

    def storeParameters(self):
        """
        _storeParameters_

        Store the original parameters in the decorator
        """
        for param in self.validParameters:
            globalValue = getattr(self.obj, param, None)
            setattr(self, param, globalValue)
        return

    def alterParameters(self, taskConf):
        """
        _alterParameters_

        Alter the parameters with the specific task configuration
        """
        for param in self.validParameters:
            # if task arg is None or 0 or "", then reuse the global one
            taskValue = getattr(self, param)
            if taskConf.get(self.validParameters[param]):
                taskValue = taskConf[self.validParameters[param]]
            setattr(self.obj, param, taskValue)
        return

    def restoreParameters(self):
        """
        _restoreParameters_

        Restore the parameters to the global values
        """
        for param in self.validParameters:
            globalValue = getattr(self, param)
            setattr(self.obj, param, globalValue)
        return

    def resetParameters(self):
        """
        _resetParameters_

        Reset parameters to None
        """
        for param in self.validParameters:
            setattr(self, param, None)
        return


class TaskChainWorkloadFactory(StdBase):
    def __init__(self):
        StdBase.__init__(self)
        self.mergeMapping = {}
        self.taskMapping = {}

    def __call__(self, workloadName, arguments):
        """
        _call_

        Create a ReReco workload with the given parameters.
        """
        StdBase.__call__(self, workloadName, arguments)
        self.workload = self.createWorkload()

        # Detect blow-up factor from first task in chain.
        blowupFactor = 1
        if (self.taskChain > 1) and 'TimePerEvent' in arguments["Task1"]:
            origTpe = arguments["Task1"]['TimePerEvent']
            if origTpe <= 0:
                origTpe = 1.0
            sumTpe = 0
            tpeCount = 0
            for i in xrange(1, self.taskChain + 1):
                if 'TimePerEvent' in arguments["Task%d" % i]:
                    sumTpe += arguments["Task%d" % i]['TimePerEvent']
                    tpeCount += 1
            if tpeCount > 0:
                blowupFactor = sumTpe / origTpe

        for i in xrange(1, self.taskChain + 1):

            originalTaskConf = arguments["Task%d" % i]
            taskConf = {}
            # Make a shallow copy of the taskConf
            for k, v in list(originalTaskConf.items()):
                taskConf[k] = v
            parent = taskConf.get("InputTask", None)

            self.modifyTaskConfiguration(taskConf, i == 1, i == 1 and 'InputDataset' not in taskConf)

            # Set task-specific global parameters
            self.blockBlacklist = taskConf["BlockBlacklist"]
            self.blockWhitelist = taskConf["BlockWhitelist"]
            self.runBlacklist = taskConf["RunBlacklist"]
            self.runWhitelist = taskConf["RunWhitelist"]

            parentTask = None
            if parent in self.mergeMapping:
                parentTask = self.mergeMapping[parent][parentTaskModule(taskConf)]

            task = self.makeTask(taskConf, parentTask)

            if i == 1:
                # First task will either be generator or processing
                self.workload.setDashboardActivity("relval")
                if isGenerator(arguments):
                    # generate mc events
                    self.workload.setWorkQueueSplitPolicy("MonteCarlo", taskConf['SplittingAlgo'],
                                                          taskConf['SplittingArguments'],
                                                          blowupFactor=blowupFactor)
                    self.workload.setEndPolicy("SingleShot")
                    self.setupGeneratorTask(task, taskConf)
                else:
                    # process an existing dataset
                    self.workload.setWorkQueueSplitPolicy("Block", taskConf['SplittingAlgo'],
                                                          taskConf['SplittingArguments'],
                                                          blowupFactor=blowupFactor)
                    self.setupTask(task, taskConf)
            else:
                # all subsequent tasks have to be processing tasks
                self.setupTask(task, taskConf)
            self.taskMapping[task.name()] = taskConf

        self.workload.ignoreOutputModules(self.ignoredOutputModules)
        self.reportWorkflowToDashboard(self.workload.getDashboardActivity())

        return self.workload

    def makeTask(self, taskConf, parentTask=None):
        """
        _makeTask_

        create new Task and populate it with basic required parameters from the
        taskConfig provided, if parentTask is None, the task will be created in
        the workload, else the task will be created as a child of the parent Task

        """
        if parentTask is None:
            task = self.workload.newTask(taskConf['TaskName'])
        else:
            task = parentTask.addTask(taskConf['TaskName'])
        return task

    def _updateCommonParams(self, task, taskConf):
        # sets the prepID  all the properties need to be set by
        # self.workload.setTaskPropertiesFromWorkload manually for the task
        task.setPrepID(taskConf.get("PrepID", self.workload.getPrepID()))
        task.setAcquisitionEra(taskConf.get("AcquisitionEra", self.workload.getAcquisitionEra()))
        task.setProcessingString(taskConf.get("ProcessingString", self.workload.getProcessingString()))
        task.setProcessingVersion(taskConf.get("ProcessingVersion", self.workload.getProcessingVersion()))
        lumiMask = taskConf.get("LumiList", self.workload.getLumiList())
        if lumiMask:
            task.setLumiMask(lumiMask)

        if taskConf["PileupConfig"]:
            self.setupPileup(task, taskConf['PileupConfig'])

    @ParameterStorage
    def setupGeneratorTask(self, task, taskConf):
        """
        _setupGeneratorTask_

        Set up an initial generation task
        """
        cmsswStepType = "CMSSW"
        configCacheID = taskConf['ConfigCacheID']
        splitAlgorithm = taskConf["SplittingAlgo"]
        splitArguments = taskConf["SplittingArguments"]
        keepOutput = taskConf["KeepOutput"]
        transientModules = taskConf["TransientOutputModules"]
        forceUnmerged = (not keepOutput) or (len(transientModules) > 0)
        cmsswVersion = taskConf.get('CMSSWVersion', self.frameworkVersion)
        scramArch = taskConf.get('ScramArch', self.scramArch)
        self.inputPrimaryDataset = taskConf['PrimaryDataset']
        outputMods = self.setupProcessingTask(task, "Production", couchDBName=self.couchDBName,
                                              configDoc=configCacheID, configCacheUrl=self.configCacheUrl,
                                              splitAlgo=splitAlgorithm, splitArgs=splitArguments,
                                              stepType=cmsswStepType, seeding=taskConf['Seeding'],
                                              totalEvents=taskConf['RequestNumEvents'],
                                              forceUnmerged=forceUnmerged,
                                              timePerEvent=taskConf.get('TimePerEvent', None),
                                              sizePerEvent=taskConf.get('SizePerEvent', None),
                                              memoryReq=taskConf.get('Memory', None),
                                              cmsswVersion=cmsswVersion,
                                              scramArch=scramArch,
                                              taskConf=taskConf)

        self.addLogCollectTask(task, 'LogCollectFor%s' % task.name(), cmsswVersion=cmsswVersion, scramArch=scramArch)

        # Do the output module merged/unmerged association
        self.setUpMergeTasks(task, outputMods, splitAlgorithm, keepOutput, transientModules,
                             cmsswVersion=cmsswVersion, scramArch=scramArch)

        # this need to be called after setpuProcessingTask since it will overwrite some values
        self._updateCommonParams(task, taskConf)

        return

    @ParameterStorage
    def setupTask(self, task, taskConf):
        """
        _setupTask_

        Build the task using the setupProcessingTask from StdBase
        and set the parents appropriately to handle a processing task
        """

        cmsswStepType = "CMSSW"
        configCacheID = taskConf["ConfigCacheID"]
        splitAlgorithm = taskConf["SplittingAlgo"]
        splitArguments = taskConf["SplittingArguments"]
        keepOutput = taskConf["KeepOutput"]
        transientModules = taskConf["TransientOutputModules"]
        forceUnmerged = (not keepOutput) or (len(transientModules) > 0)
        cmsswVersion = taskConf.get('CMSSWVersion', self.frameworkVersion)
        scramArch = taskConf.get('ScramArch', self.scramArch)

        # in case the initial task is a processing task, we have an input dataset, otherwise
        # we look up the parent task and step
        inputDataset = taskConf.get("InputDataset")
        if inputDataset is not None:
            self.inputDataset = inputDataset
            (self.inputPrimaryDataset, self.inputProcessedDataset,
             self.inputDataTier) = self.inputDataset[1:].split("/")
            inpStep = None
            inpMod = None
        else:
            self.inputDataset = None
            inputTask = taskConf["InputTask"]
            inputTaskConf = self.taskMapping[inputTask]
            parentTaskForMod = self.mergeMapping[inputTask][taskConf['InputFromOutputModule']]
            inpStep = parentTaskForMod.getStep("cmsRun1")
            if not inputTaskConf["KeepOutput"] or len(inputTaskConf["TransientOutputModules"]) > 0:
                inpMod = taskConf["InputFromOutputModule"]
                # Check if the splitting has to be changed
                if inputTaskConf["SplittingAlgo"] == 'EventBased' \
                        and (inputTaskConf.get("InputDataset") or inputTaskConf.get("InputTask")):
                    splitAlgorithm = 'WMBSMergeBySize'
                    splitArguments = {'max_merge_size': self.maxMergeSize,
                                      'min_merge_size': self.minMergeSize,
                                      'max_merge_events': self.maxMergeEvents,
                                      'max_wait_time': self.maxWaitTime}
            else:
                inpMod = "Merged"

        currentPrimaryDataset = self.inputPrimaryDataset
        if taskConf.get("PrimaryDataset") is not None:
            self.inputPrimaryDataset = taskConf.get("PrimaryDataset")

        outputMods = self.setupProcessingTask(task, "Processing",
                                              inputDataset,
                                              inputStep=inpStep,
                                              inputModule=inpMod,
                                              couchDBName=self.couchDBName,
                                              configCacheUrl=self.configCacheUrl,
                                              configDoc=configCacheID,
                                              splitAlgo=taskConf["SplittingAlgo"],
                                              splitArgs=splitArguments,
                                              stepType=cmsswStepType,
                                              forceUnmerged=forceUnmerged,
                                              timePerEvent=taskConf.get('TimePerEvent', None),
                                              sizePerEvent=taskConf.get('SizePerEvent', None),
                                              memoryReq=taskConf.get("Memory", None),
                                              cmsswVersion=cmsswVersion,
                                              scramArch=scramArch,
                                              taskConf=taskConf)

        self.addLogCollectTask(task, 'LogCollectFor%s' % task.name(), cmsswVersion=cmsswVersion, scramArch=scramArch)
        self.setUpMergeTasks(task, outputMods, splitAlgorithm, keepOutput, transientModules,
                             cmsswVersion=cmsswVersion, scramArch=scramArch)

        self.inputPrimaryDataset = currentPrimaryDataset

        # this need to be called after setpuProcessingTask since it will overwrite some values
        self._updateCommonParams(task, taskConf)

        return

    def setUpMergeTasks(self, parentTask, outputModules, splittingAlgo,
                        keepOutput, transientOutputModules, cmsswVersion=None, scramArch=None):
        """
        _setUpMergeTasks_

        Set up the required merged tasks according to the following parameters:
        - KeepOutput : All output modules not in the transient list are merged.
        - TransientOutputModules : These output modules won't be merged.
        If not merged then only a cleanup task is created.
        """
        modulesToMerge = []
        unmergedModules = list(outputModules.keys())
        if keepOutput:
            unmergedModules = [x for x in list(outputModules.keys()) if x in transientOutputModules]
            modulesToMerge = [x for x in list(outputModules.keys()) if x not in transientOutputModules]

        procMergeTasks = {}
        for outputModuleName in modulesToMerge:
            mergeTask = self.addMergeTask(parentTask, splittingAlgo,
                                          outputModuleName, cmsswVersion=cmsswVersion, scramArch=scramArch)
            procMergeTasks[str(outputModuleName)] = mergeTask
        self.mergeMapping[parentTask.name()] = procMergeTasks

        procTasks = {}
        for outputModuleName in unmergedModules:
            self.addCleanupTask(parentTask, outputModuleName, dataTier=outputModules[outputModuleName]['dataTier'])
            procTasks[outputModuleName] = parentTask
        self.mergeMapping[parentTask.name()].update(procTasks)

        return

    def modifyTaskConfiguration(self, taskConf,
                                firstTask=False, generator=False):
        """
        _modifyTaskConfiguration_

        Modify the TaskConfiguration according to the specifications
        in getWorkloadCreateArgs and getChainCreateArgs.
        It does type casting and assigns default values if key is not
        present, unless default value is None.
        """
        taskArguments = self.getChainCreateArgs(firstTask, generator)
        for argument in taskArguments:
            if argument not in taskConf and taskArguments[argument]["default"] is not None:
                taskConf[argument] = taskArguments[argument]["default"]
            elif argument in taskConf:
                taskConf[argument] = taskArguments[argument]["type"](taskConf[argument])

        if generator:
            taskConf["SplittingAlgo"] = "EventBased"
            # Adjust totalEvents according to the filter efficiency
            taskConf["RequestNumEvents"] = int(taskConf.get("RequestNumEvents", 0) / \
                                               taskConf.get("FilterEfficiency"))
            taskConf["SizePerEvent"] = taskConf.get("SizePerEvent", self.sizePerEvent) * \
                                       taskConf.get("FilterEfficiency")

        taskConf["SplittingArguments"] = {}
        if taskConf["SplittingAlgo"] in ["EventBased", "EventAwareLumiBased"]:
            if taskConf.get("EventsPerJob") is None:
                taskConf["EventsPerJob"] = int((8.0 * 3600.0) / taskConf.get("TimePerEvent", self.timePerEvent))
            if taskConf.get("EventsPerLumi") is None:
                taskConf["EventsPerLumi"] = taskConf["EventsPerJob"]
            taskConf["SplittingArguments"]["events_per_job"] = taskConf["EventsPerJob"]
            if taskConf["SplittingAlgo"] == "EventAwareLumiBased":
                taskConf["SplittingArguments"]["max_events_per_lumi"] = 20000
            else:
                taskConf["SplittingArguments"]["events_per_lumi"] = taskConf["EventsPerLumi"]
            taskConf["SplittingArguments"]["lheInputFiles"] = taskConf["LheInputFiles"]
        elif taskConf["SplittingAlgo"] == "LumiBased":
            taskConf["SplittingArguments"]["lumis_per_job"] = taskConf["LumisPerJob"]
        elif taskConf["SplittingAlgo"] == "FileBased":
            taskConf["SplittingArguments"]["files_per_job"] = taskConf["FilesPerJob"]

        taskConf["PileupConfig"] = parsePileupConfig(taskConf.get("MCPileup"),
                                                     taskConf.get("DataPileup"))
        # Adjust the pileup splitting
        taskConf["SplittingArguments"].setdefault("deterministicPileup", taskConf['DeterministicPileup'])

        return

    @staticmethod
    def getWorkloadCreateArgs():
        baseArgs = StdBase.getWorkloadCreateArgs()
        specArgs = {"RequestType": {"default": "TaskChain", "optional": False},
                    "Task1": {"default": {}, "optional": False, "type": dict},
                    # ConfigCacheID is not used in the main dict for TaskChain
                    "ConfigCacheID": {"optional": True, "null": True},
                    "IgnoredOutputModules": {"default": [], "type": makeList, "null": False},
                    "TaskChain": {"default": 1, "type": int,
                                  "optional": False, "validate": lambda x: x > 0,
                                  "attr": "taskChain", "null": False},
                    "FirstEvent": {"default": 1, "type": int,
                                   "optional": True, "validate": lambda x: x > 0,
                                   "attr": "firstEvent", "null": False},
                    "FirstLumi": {"default": 1, "type": int,
                                  "optional": True, "validate": lambda x: x > 0,
                                  "attr": "firstLumi", "null": False}
                   }
        baseArgs.update(specArgs)
        StdBase.setDefaultArgumentsProperty(baseArgs)
        return baseArgs

    @staticmethod
    def getChainCreateArgs(firstTask=False, generator=False):
        """
        _getChainCreateArgs_

        Each task dictionary specifies its own set of arguments
        that need to be validated as well, most of them are already
        defined in StdBase.getWorkloadCreateArgs and those do not appear here
        since they are all optional. Here only new arguments are listed.
        """
        baseArgs = StdBase.getChainCreateArgs(firstTask, generator)
        arguments = {
            "TaskName": {"optional": False, "null": False},
            "InputTask": {"default": None, "optional": firstTask, "null": False},
            "TransientOutputModules": {"default": [], "type": makeList, "optional": True, "null": False},
            "DeterministicPileup": {"default": False, "type": strToBool, "optional": True, "null": False},
            "GlobalTag": {"type": str, "optional": True},
            "TimePerEvent": {"type": float, "optional": True, "validate": lambda x: x > 0},
            "SizePerEvent": {"type": float, "optional": True, "validate": lambda x: x > 0},
            'PrimaryDataset': {'default': None, 'optional': not generator, 'validate': primdataset,
                               'null': False},
                    }
        baseArgs.update(arguments)
        StdBase.setDefaultArgumentsProperty(baseArgs)
        return baseArgs

    def validateSchema(self, schema):
        """
        _validateSchema_

        Go over each task and make sure it matches validation
        parameters derived from Dave's requirements.
        """
        numTasks = schema['TaskChain']
        transientMapping = {}
        for i in xrange(1, numTasks + 1):
            taskNumber = "Task%s" % i
            if taskNumber not in schema:
                msg = "No Task%s entry present in request" % i
                self.raiseValidationException(msg=msg)

            task = schema[taskNumber]
            # We can't handle non-dictionary tasks
            if not isinstance(task, dict):
                msg = "Non-dictionary input for task in TaskChain.\n"
                msg += "Could be an indicator of JSON error.\n"
                self.raiseValidationException(msg=msg)

            # Generic task parameter validation
            self.validateTask(task, self.getChainCreateArgs(i == 1, i == 1 and 'InputDataset' not in task))

            # Validate the existence of the configCache
            if task["ConfigCacheID"]:
                self.validateConfigCacheExists(configID=task['ConfigCacheID'],
                                               configCacheUrl=schema["ConfigCacheUrl"],
                                               couchDBName=schema["CouchDBName"],
                                               getOutputModules=False)

            # Validate the chaining of transient output modules, need to make a copy of the lists
            transientMapping[task['TaskName']] = [x for x in task.get('TransientOutputModules', [])]

            if i > 1:
                inputTransientModules = transientMapping[task['InputTask']]
                if task['InputFromOutputModule'] in inputTransientModules:
                    inputTransientModules.remove(task['InputFromOutputModule'])

        for task in transientMapping:
            if transientMapping[task]:
                msg = "A transient module is not processed by a subsequent task.\n"
                msg += "This is a malformed task chain workload"
                self.raiseValidationException(msg)

    def validateTask(self, taskConf, taskArgumentDefinition):
        """
        _validateTask_

        Validate the task information against the given
        argument description
        """
        try:
            validateArgumentsCreate(taskConf, taskArgumentDefinition, checkInputDset=False)
        except Exception as ex:
            self.raiseValidationException(str(ex))

        return
