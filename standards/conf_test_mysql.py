#!/usr/bin/env python

from WMQuality.Test import Test


from WMCore_t.WMException_t import WMExceptionTest        
from WMComponent_t.DBSBuffer_t.DBSBuffer_t import DBSBufferTest
from WMComponent_t.DBSUpload_t.DBSUpload_t import DBSUploadTest
from WMComponent_t.ErrorHandler_t.ErrorHandler_t import ErrorHandlerTest
from WMComponent_t.Proxy_t.Proxy_t import ProxyTest

from WMCore_t.FwkJobReport_t.FileInfo_t import FileInfoTest
from WMCore_t.FwkJobReport_t.FJR_t import FJRTest
#from WMCore_t.HTTPFrontEnd_t.REST_t.Rest_t import RestTest
from WMCore_t.ThreadPool_t.ThreadPool_t import ThreadPoolTest
from WMCore_t.MsgService_t.MsgService_t import MsgServiceTest
from WMCore_t.Agent_t.Configuration_t import ConfigurationTest
from WMCore_t.Agent_t.Daemon_t.Daemon_t import DaemonTest
from WMCore_t.Database_t.DBFormatter_t import DBFormatterTest
from WMCore_t.Agent_t.Harness_t import HarnessTest
from WMCore_t.Trigger_t.Trigger_t import TriggerTest
from WMCore_t.WMFactory_t.WMFactory_t import WMFactoryTest

tests = [\
     (FileInfoTest(), 'evansde'),\
     (FJRTest(), 'evansde'),\
     (WMFactoryTest(), 'fvlingen'),\
     (ThreadPoolTest(),'fvlingen'),\
     (TriggerTest(),'fvlingen'),\
     (WMExceptionTest(),'fvlingen'),\
     (ConfigurationTest(),'fvlingen'),\
     (DBFormatterTest(),'fvlingen'),\
     (HarnessTest(),'fvlingen'),\
     #(DBSUploadTest(),'afaq'),\
     #(DBSBufferTest(),'afaq'),\
     #(RestTest(), 'valya'),\
     (ErrorHandlerTest(),'fvlingen'),\
     (MsgServiceTest(),'fvlingen'),\
     (ProxyTest(),'fvlingen'),\
     (DaemonTest(),'fvlingen'),\
    ]

test = Test(tests)
test.run()
test.summaryText()
        
