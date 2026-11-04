import json, os, sys, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
import shutil
from robbi.app_runtime.sandbox import run


class Context:
    def __init__(self):
        self.app_id = "probe"
        self.config = {}
        self.permissions = []
        self.deadline = time.monotonic() + 8
        self.robot = SimpleNamespace(imu=lambda: {"heading": 12})

    def checkpoint(self):
        if time.monotonic() >= self.deadline:
            raise RuntimeError("expired")


@unittest.skipUnless(
    sys.platform.startswith("linux") and shutil.which("bwrap"),
    "Requires Linux bubblewrap",
)
class SandboxTest(unittest.TestCase):
    def invoke(self, code, context=None):
        with tempfile.TemporaryDirectory(prefix="robbi-sandbox-") as p:
            root = Path(p)
            (root / "main.py").write_text(code)
            (root / "helper.py").write_text("message='relative imports work'\n")
            project = SimpleNamespace(
                id="probe", root=root, manifest={"entrypoint": "main:run"}
            )
            return run(project, context or Context(), "run", {})

    def test_success_and_relative_import(self):
        result = self.invoke(
            "from .helper import message\ndef run(c,t,a):\n print('not protocol')\n return {'ok':True,'text':message,'imu':c.robot.imu()}\n"
        )
        self.assertEqual(result["text"], "relative imports work")
        self.assertEqual(result["imu"], {"heading": 12})

    def test_denies_host_files_devices_network_forks_and_exec(self):
        os.environ["ROBBI_QA_SECRET"] = "never-expose"
        code = """import os, socket, subprocess
from pathlib import Path
def run(c,t,a):
 result={}
 for name,fn in {'env':lambda:os.environ['ROBBI_QA_SECRET'], 'host':lambda:Path('/etc/passwd').read_text(), 'devices':lambda:open('/dev/gpiomem','rb'), 'network':lambda:socket.socket(), 'fork':lambda:os.fork(), 'exec':lambda:subprocess.run(['/usr/bin/true']), 'write':lambda:Path('/app/new.py').write_text('bad'), 'root_write':lambda:Path('/unbounded').write_text('bad'), 'shm_write':lambda:Path('/dev/shm/unbounded').write_text('bad'), 'memfd':lambda:os.memfd_create('unbounded')}.items():
  try:fn();result[name]='ESCAPE'
  except (OSError,KeyError):result[name]='blocked'
 return {'ok':True,'checks':result}
"""
        result = self.invoke(code)
        self.assertTrue(all(v == "blocked" for v in result["checks"].values()), result)

    def test_deadline_kills_uncooperative_code(self):
        c = Context()
        c.deadline = time.monotonic() + 0.5
        start = time.monotonic()
        with self.assertRaises(RuntimeError):
            self.invoke("def run(c,t,a):\n while True:pass\n", c)
        self.assertLess(time.monotonic() - start, 2)

    def test_memory_limit(self):
        result = self.invoke(
            "def run(c,t,a):\n try: x=bytearray(512*1024*1024)\n except MemoryError:return {'ok':True,'bounded':True}\n return {'bounded':False}\n"
        )
        self.assertTrue(result["bounded"])

    def test_temporary_storage_is_bounded(self):
        result = self.invoke("""from pathlib import Path
def run(c,t,a):
 count=0
 try:
  for n in range(32):
   Path('/tmp/'+str(n)).write_bytes(b'x'*1024*1024)
   count+=1
 except OSError:return {'bounded':True, 'mib':count}
 return {'bounded':False}
""")
        self.assertTrue(result['bounded'])
        self.assertGreater(result['mib'], 0)
        self.assertLessEqual(result['mib'], 16)

    def test_broker_no_private_methods(self):
        with self.assertRaises(ValueError):
            self.invoke("def run(c,t,a):\n return c._call('robot._port',[],{})\n")

    def test_real_host_requires_permission_then_persists_state_without_importing(self):
        import io, zipfile, yaml
        from robbi.app_runtime.host import AppHost

        with tempfile.TemporaryDirectory(prefix="robbi-host-") as folder:
            host = AppHost(
                Path(folder) / "runtime",
                Path(folder) / "data",
                SimpleNamespace(capture_token=lambda: None),
            )
            payload = io.BytesIO()
            manifest = {
                "api_version": 2,
                "id": "probe",
                "permissions": ["state"],
                "entrypoint": "main:run",
                "tools": [
                    {
                        "name": "run",
                        "description": "Read and increment a counter",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ],
            }
            with zipfile.ZipFile(payload, "w") as z:
                z.writestr("app.yaml", yaml.safe_dump(manifest))
                z.writestr(
                    "main.py",
                    "def run(c,t,a):\n state=c.read_state();state['count']=state.get('count',0)+1;c.write_state(state);return {'ok':True,'data':state}\n",
                )
            try:
                host.scan()
                host.install(payload.getvalue())
                host.set_enabled("probe", True)
                result = host.call("probe", "run", {})
                self.assertTrue(result["permission_required"])
                self.assertFalse(host._modules)
                host.set_permissions("probe", {"state": "granted"})
                self.assertEqual(host.call("probe", "run", {})["data"], {"count": 1})
                self.assertEqual(host.call("probe", "run", {})["data"], {"count": 2})
                host.set_permissions("probe", {"state": "denied"})
                self.assertTrue(host.call("probe", "run", {})["permission_required"])
                self.assertFalse(host._modules)
            finally:
                host.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
