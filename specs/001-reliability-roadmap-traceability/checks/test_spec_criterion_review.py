import copy,importlib.util,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
spec=importlib.util.spec_from_file_location("gate",ROOT/"scripts/check_spec_criterion_review.py")
gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)
class Cases(unittest.TestCase):
    def setUp(self):self.d,self.o,self.c,self.b=gate.inputs()
    def test_valid(self):self.assertEqual(gate.validate(self.d,self.o,self.c,self.b),[])
    def test_missing(self):self.d["rows"].pop();self.assertTrue(gate.validate(self.d,self.o,self.c,self.b))
    def test_rewrite(self):self.d["rows"][0]["exact_text"]="changed";self.assertTrue(gate.validate(self.d,self.o,self.c,self.b))
    def test_source_drift(self):self.c[next(iter(self.c))]+=b"\n";self.assertTrue(gate.validate(self.d,self.o,self.c,self.b))
    def test_missing_task(self):self.b=self.b.replace(self.d["rows"][0]["task_id"],"LOST");self.assertTrue(gate.validate(self.d,self.o,self.c,self.b))
    def test_fake_completion(self):
        r=self.d["rows"][0];r["state"]="verified";self.b=self.b.replace("[ ] "+r["task_id"],"[x] "+r["task_id"])
        self.assertTrue(any("missing criterion-specific" in e for e in gate.validate(self.d,self.o,self.c,self.b)))
    def test_wrong_evidence(self):
        r=self.d["rows"][0];r["state"]="verified";self.b=self.b.replace("[ ] "+r["task_id"],"[x] "+r["task_id"]);r["acceptance_evidence"]=[{"criterion":"WRONG","outcome":"pass","source_commit":"a"*40,"artifact_sha256":"b"*64,"receipt_ref":"r","review_ref":"r"}]
        self.assertTrue(any("missing criterion-specific" in e for e in gate.validate(self.d,self.o,self.c,self.b)))
    def test_authority(self):self.d["execution_authorized"]=True;self.assertTrue(gate.validate(self.d,self.o,self.c,self.b))
