import subprocess
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
class CLITests(unittest.TestCase):
    def test_help(self):
        p=subprocess.run([str(ROOT/'poc'),'--help'],capture_output=True,text=True,check=True)
        for command in ('campaign','benchmark','mode','up','down','check'):
            self.assertIn(command,p.stdout)
    def test_campaign_help(self):
        p=subprocess.run([str(ROOT/'poc'),'campaign','--help'],capture_output=True,text=True,check=True)
        self.assertIn('--only',p.stdout)
        for flag in ('--strategies','--rates','--repetitions','--writes','--max-vus',
                     '--fault','--fault-at','--recover-at','capacity','resilience'):
            self.assertIn(flag,p.stdout)
