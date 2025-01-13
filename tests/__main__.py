import os
import sys
import unittest
import logging
import xmlrunner
from pathlib import Path


logging.basicConfig(
    level = logging.INFO,
    format='%(created)f:%(levelname)s: %(message)s'
)

suite = unittest.TestSuite()
current_dir = Path(__file__).parent
loader = unittest.TestLoader()
package_tests = loader.discover(
    start_dir=current_dir.parent, pattern="*test.py")
suite.addTests(package_tests)
runner = xmlrunner.XMLTestRunner(output='test-reports')
rtn = not runner.run(suite).wasSuccessful()
sys.exit(rtn)
