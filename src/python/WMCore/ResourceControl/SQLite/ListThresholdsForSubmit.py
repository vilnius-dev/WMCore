#!/usr/bin/env python
"""
_ListThresholdsForSubmit_

SQLite implementation of ResourceControl.ListThresholdsForSubmit
"""

__revision__ = "$Id: ListThresholdsForSubmit.py,v 1.1 2010/02/09 17:57:03 sfoulkes Exp $"
__version__  = "$Revision: 1.1 $"

from WMCore.ResourceControl.MySQL.ListThresholdsForSubmit \
     import ListThresholdsForSubmit as MySQLListThresholdsForSubmit

class ListThresholdsForSubmit(MySQLListThresholdsForSubmit):
    pass
