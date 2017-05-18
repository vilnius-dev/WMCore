"""
Unlike ReqMgr1 defining Request and RequestSchema classes,
define just 1 class. Derived from Python dict and implementing
necessary conversion and validation extra methods possibly needed.

TODO/NOTE:
    'inputMode' should be removed by now (2013-07)

    since arguments validation #4705, arguments which are later
        validated during spec instantiation and which are not
        present in the request injection request, can't be defined
        here because their None value is not allowed in the spec.
        This is the case for e.g. DbsUrl, AcquisitionEra
        This module should probably define only absolutely
        necessary request parameters and not any optional ones.

"""
from __future__ import print_function, division
import time
import re
import cherrypy
from WMCore.ReqMgr.DataStructs.RequestStatus import REQUEST_START_STATE, ACTIVE_STATUS_FILTER

# TODO: I wish we can, one day, remove this stuff and have a decent resubmission handling... :)
ARGS_TO_REMOVE_FROM_ORIGINAL_REQUEST = \
    ['DN', 'Dashboard', 'DeleteFromSource', 'EnableNewStageout', 'GracePeriod',
     'HardTimeout', 'IgnoredOutputModules', 'InitialPriority', 'MaxRSS', 'MaxVSize',
     'MaxWaitTime', 'OutputDatasets', 'OutputModulesLFNBases', 'ReqMgr2Only',
     'RequestStatus', 'RequestTransition', 'RequestWorkflow', 'Requestor', 'RequestorDN',
     'SiteBlacklist', 'SiteWhitelist', 'SoftTimeout', 'SoftwareVersions', 'Team',
     'Teams', 'TotalEstimatedJobs', 'TotalInputEvents', 'TotalInputFiles', 'TotalInputLumis',
     'TransientOutputModules', 'TrustPUSitelists', 'TrustSitelists', 'VoRole', '_id', '_rev']


def initialize_request_args(request, config):
    """
    Request data class request is a dictionary representing
    a being injected / created request. This method initializes
    various request fields. This should be the ONLY method to
    manipulate request arguments upon injection so that various
    levels or arguments manipulation does not occur across several
    modules and across about 7 various methods like in ReqMgr1.

    request is changed here.
    """

    # user information for cert. (which is converted to cherry py log in)
    request["Requestor"] = cherrypy.request.user["login"]
    request["RequestorDN"] = cherrypy.request.user.get("dn", "unknown")
    # service certificates carry @hostname, remove it if it exists
    request["Requestor"] = request["Requestor"].split('@')[0]

    # assign first starting status, should be 'new'
    request["RequestStatus"] = REQUEST_START_STATE
    request["RequestTransition"] = [{"Status": request["RequestStatus"],
                                     "UpdateTime": int(time.time()), "DN": request["RequestorDN"]}]
    request["RequestDate"] = list(time.gmtime()[:6])

    # update the information from config
    request["CouchURL"] = config.couch_host
    request["CouchWorkloadDBName"] = config.couch_reqmgr_db
    request["CouchDBName"] = config.couch_config_cache_db

    generateRequestName(request)


def _replace_cloned_args(clone_args, user_args):
    """
    replace original arguments with user argument.
    If the value is dictionary format, overwrite only with specified arguments.
    If the original argument has simple value and user passes dictionary, completely replace to dictionary
    XXX this means LumiList won't remove other runs, only updates runs overwritten
    """
    for prop in user_args:
        if isinstance(user_args[prop], dict) and isinstance(clone_args.get(prop), dict):
            _replace_cloned_args(clone_args.get(prop, {}), user_args[prop])
        else:
            clone_args[prop] = user_args[prop]
    return


def initialize_clone(requestArgs, originalArgs, argsDefinition, chainDefinition=None):
    """
    Initialize arguments for a clone request by inheriting and overwriting argument
    from OriginalRequest.

    :param requestArgs: user-provided dictionary with override arguments
    :param originalArgs: original arguments retrieved for the workflow being cloned
    :param argsDefinition: arguments definition according to the workflow type being cloned
    :param chainDefinition: a dictionary containing the chain argument definition, for
    StepChain and TaskChain
    :return: dictionary with original args filtered out, as per the spec definition. And on
     top of that, user arguments added/replaced in the dictionary.
    """
    chainPattern = r'(Task|Step)\d{1,2}'
    cloneArgs = {}
    for topKey, topValue in originalArgs.iteritems():
        if topKey in argsDefinition:
            cloneArgs[topKey] = topValue
        elif topKey.startswith(("Skim", "Step", "Task")):
            # accept floating args from ReReco, StepChain and TaskChain
            if re.match(chainPattern, topKey):
                for innerKey in topValue:
                    if innerKey not in chainDefinition:
                        # remove internal keys that are not in the spec
                        topValue.pop(innerKey, None)
            cloneArgs[topKey] = topValue

    # apply user override arguments at the end, such that it's validated at spec level
    _replace_cloned_args(cloneArgs, requestArgs)

    return cloneArgs


def generateRequestName(request):
    currentTime = time.strftime('%y%m%d_%H%M%S', time.localtime(time.time()))
    seconds = int(10000 * (time.time() % 1.0))

    request["RequestName"] = "%s_%s" % (request["Requestor"], request.get("RequestString"))
    request["RequestName"] += "_%s_%s" % (currentTime, seconds)


def protectedLFNs(requestInfo):
    reqData = RequestInfo(requestInfo)
    result = []
    if reqData.andFilterCheck(ACTIVE_STATUS_FILTER):
        outs = requestInfo.get('OutputDatasets', [])
        base = requestInfo.get('UnmergedLFNBase', '/store/unmerged')
        for out in outs:
            dsn, ps, tier = out.split('/')[1:]
            acq, rest = ps.split('-', 1)
            dirPath = '/'.join([base, acq, dsn, tier, rest])
            result.append(dirPath)
    return result


class RequestInfo(object):
    """
    Wrapper class for Request data
    """

    def __init__(self, requestData):
        self.data = requestData

    def _maskTaskStepChain(self, prop, chain_name, default=None):

        propExist = False
        numLoop = self.data["%sChain" % chain_name]
        for i in range(numLoop):
            if prop in self.data["%s%s" % (chain_name, i + 1)]:
                propExist = True
                break

        defaultValue = self.data.get(prop, default)

        if propExist:
            result = set()
            for i in range(numLoop):
                chain_key = "%s%s" % (chain_name, i + 1)
                chain = self.data[chain_key]
                if prop in chain:
                    result.add(chain[prop])
                else:
                    if isinstance(defaultValue, dict):
                        value = defaultValue.get(chain_key, None)
                    else:
                        value = defaultValue

                    if value is not None:
                        result.add(value)
            return list(result)
        else:
            # property which can't be task or stepchain property but in dictionary format
            exculdePropWithDictFormat = ["LumiList", "AgentJobInfo"]
            if prop not in exculdePropWithDictFormat and isinstance(defaultValue, dict):
                return defaultValue.values()
            else:
                return defaultValue

        return

    def get(self, prop, default=None):
        """
        gets the value when prop exist as one of the properties in the request document.
        In case TaskChain, StepChain workflow it searches the property in Task/Step level
        """

        if "TaskChain" in self.data:
            return self._maskTaskStepChain(prop, "Task")
        elif "StepChain" in self.data:
            return self._maskTaskStepChain(prop, "Step")
        elif prop in self.data:
            return self.data[prop]
        else:
            return default

    def andFilterCheck(self, filterDict):
        """
        checks whether filterDict condition met.
        filterDict is the dict of key and value(list) format)
        i.e.
        {"RequestStatus": ["running-closed", "completed"],}
        If this request's RequestStatus is either "running-closed", "completed",
        return True, otherwise False
        """
        for key, value in filterDict.iteritems():
            # special case checks where key is not exist in Request's Doc.
            # It is used whether AgentJobInfo is deleted or not for announced status
            if value == "CLEANED" and key == "AgentJobInfo":
                if self.isWorkflowCleaned():
                    continue
                else:
                    return False

            if isinstance(value, dict):
                # TODO: need to handle dictionary comparison
                # For now ignore
                continue
            elif not isinstance(value, list):
                value = [value]

            reqValue = self.get(key)
            if reqValue is not None:
                if isinstance(reqValue, list):
                    if not set(reqValue).intersection(set(value)):
                        return False
                elif reqValue not in value:
                    return False
            else:
                return False
        return True

    def isWorkflowCleaned(self):
        """
        check whether workflow data is cleaned up from agent only checks the couchdb
        Since dbsbuffer data is not clean up we can't just check 'AgentJobInfo' key existence
        This all is only meaningfull if request status is right befor end status.
        ["aborted-completed", "rejected", "announced"]
        DO NOT check if workflow status isn't among those status
        """
        if 'AgentJobInfo' in self.data:
            for agentRequestInfo in self.data['AgentJobInfo'].values():
                if agentRequestInfo.get("status", {}):
                    return False
        # cannot determin whether AgentJobInfo is cleaned or not when 'AgentJobInfo' Key doesn't exist
        # Maybe JobInformation is not included but since it requested by above status assumed it returns True
        return True
