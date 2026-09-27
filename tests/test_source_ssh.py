import base64
import copy
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_source_ssh as ssh
import homestead_imports as imports
import homestead_operations as ops
import homestead_capacity_review as review
import server
from fixtures_source import source


class SourceSSHTests(unittest.TestCase):
    def setUp(self):
        self.src = source()

    def test_unknown_or_changed_endpoint_never_builds_authenticated_command(self):
        for mutate in (lambda s:s.pop("ssh_trust"),lambda s:s.update(host="192.0.2.11"),lambda s:s.update(user="other"),lambda s:s.update(port=2222)):
            src=copy.deepcopy(self.src);mutate(src)
            with self.assertRaises(ValueError):
                ssh.command(src,"echo hello")

    def test_commands_pin_key_without_password_in_arguments_or_auto_accept(self):
        script=ssh.setup(self.src)+ssh.command(self.src,"echo 'hello'")
        self.assertIn("StrictHostKeyChecking=yes",script)
        self.assertIn("UpdateHostKeys=no",script)
        self.assertIn("GlobalKnownHostsFile=/dev/null",script)
        self.assertIn("sshpass -e",script)
        self.assertNotIn("StrictHostKeyChecking=no",script)
        self.assertNotIn("$SRC_PASS",script)
        self.assertIn(self.src["ssh_trust"]["key"],script)

    def test_scan_is_passwordless_and_returns_one_key_to_compare(self):
        self.assertNotIn("sshpass",ssh.scan_script(self.src))
        found=ssh.candidate(["# ignored", "192.0.2.10 "+self.src["ssh_trust"]["key"]])
        self.assertEqual(self.src["ssh_trust"]["fingerprint"],found["fingerprint"])
        self.assertEqual("ssh-ed25519",found["algorithm"])

    def test_malformed_or_ambiguous_keys_are_rejected(self):
        key=self.src["ssh_trust"]["key"]
        other=key.split()[0]+" "+base64.b64encode(base64.b64decode(key.split()[1])[:-1]+b"X").decode()
        for values in ([],["host ssh-ed25519 invalid"],["host "+key,"host "+other]):
            with self.assertRaises(ValueError):ssh.candidate(values)
        for bad in ("ssh-rsa "+key.split()[1],key+" injected", "ssh-ed25519 AA=="):
            with self.assertRaises(ValueError):ssh.key(bad)

    def test_endpoint_rejects_options_urls_and_shell_input(self):
        for host in ("-oProxyCommand=bad","host;whoami","https://host/","name@host","host\nother","host:22"):
            with self.assertRaises(ValueError):ssh.connection({"host":host,"user":"root"})
        for user in ("-root","a;id","name@host"):
            with self.assertRaises(ValueError):ssh.connection({"host":"192.0.2.10","user":user})
        self.assertEqual(2222,ssh.connection({"host":"2001:db8::1","user":"root","port":"2222"})["port"])
        for port in (True, 22.5, 0, 65536, "22;id", None):
            with self.assertRaises(ValueError):ssh.connection({**self.src,"port":port})

    def test_source_check_requires_exact_stopped_unpaused_nonrestarting_container(self):
        cfg={"source_container_id":"a"*64,"source_consistency":"stopped"}
        script=ssh.source_check(self.src,cfg)
        self.assertIn("false false false",script);self.assertIn("docker inspect",script)
        self.assertNotIn("docker stop",script)
        self.assertEqual("",ssh.source_check(self.src,{**cfg,"source_consistency":"snapshot"}))
        with self.assertRaises(ValueError):ssh.source_check(self.src,{**cfg,"source_container_id":"a; touch /tmp/oops"})


class SourceTrustTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.src=source();self.src.pop("ssh_trust")
        self.cm={"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"homestead-sources","namespace":"lab","uid":"cm","resourceVersion":"1"},
                 "data":{"sources.json":json.dumps([self.src])}}
        self.sent=[]
        for patch in (mock.patch.object(ops,"DATA_DIR",tmp.name),mock.patch.object(review,"_key",return_value=b"ssh-source-tests"),
                      mock.patch.object(imports,"NS","lab"),mock.patch.object(imports,"_sources_map",return_value="homestead-sources"),
                      mock.patch.object(imports,"kget",side_effect=lambda path:copy.deepcopy(self.cm)),
                      mock.patch.object(imports,"ksend",side_effect=self.send)):
            patch.start();self.addCleanup(patch.stop)

    def send(self,method,path,body):
        self.sent.append((method,path,copy.deepcopy(body)))
        if body["kind"]=="ConfigMap":
            self.assertEqual(self.cm["metadata"]["resourceVersion"],body["metadata"]["resourceVersion"])
            self.cm=copy.deepcopy(body);self.cm["metadata"]["resourceVersion"]=str(int(self.cm["metadata"]["resourceVersion"])+1)
        return body

    def scanned(self):
        with mock.patch.object(imports,"run_probe",return_value=["192.0.2.10 "+source()["ssh_trust"]["key"]]) as probe:
            result=imports.scan_source("tower","admin")
        self.assertFalse(probe.call_args.kwargs["authenticated"])
        self.assertEqual([],self.sent)
        return {**result,"confirm_fingerprint":True}

    def test_existing_sources_require_explicit_bound_confirmation(self):
        body=self.scanned()
        with self.assertRaises(ValueError):imports.trust_source({**body,"confirm_fingerprint":False},"admin")
        with self.assertRaises(ValueError):imports.trust_source(body,"other-admin")
        self.assertEqual([],self.sent)
        result=imports.trust_source(body,"admin")
        self.assertEqual(body["fingerprint"],result["sources"][0]["ssh_trust"]["fingerprint"])
        with self.assertRaises(ValueError):imports.trust_source(body,"admin")

    def test_changed_inventory_or_key_invalidates_confirmation(self):
        body=self.scanned()
        with self.assertRaises(ValueError):imports.trust_source({**body,"key":body["key"].replace("AAA", "BBB")},"admin")
        self.cm["metadata"]["resourceVersion"]="2"
        with self.assertRaises(ValueError):imports.trust_source(body,"admin")
        self.assertEqual([],self.sent)

    def test_expired_confirmation_cannot_save_trust(self):
        body=self.scanned()
        with mock.patch.object(review.time,"time",return_value=int(body["capacity_token"].split(".")[0])+1):
            with self.assertRaises(ValueError):imports.trust_source(body,"admin")
        self.assertEqual([],self.sent)

    def test_uncertain_credential_creation_never_changes_directory(self):
        with mock.patch.object(imports,"ksend",side_effect=TimeoutError("unknown")) as send:
            with self.assertRaises(TimeoutError):imports.add_source("new","192.0.2.11","root","fixture-password")
        self.assertEqual(1,send.call_count)
        self.assertEqual([self.src],json.loads(self.cm["data"]["sources.json"]))

    def test_directory_cas_conflict_is_not_retried(self):
        body=self.scanned()
        with mock.patch.object(imports,"ksend",side_effect=urllib.error.HTTPError("",409,"",{},None)) as send:
            with self.assertRaises(urllib.error.HTTPError):imports.trust_source(body,"admin")
        self.assertEqual(1,send.call_count)
        self.assertNotIn("ssh_trust",json.loads(self.cm["data"]["sources.json"])[0])

    def test_new_source_never_reuses_a_credential_secret_or_sends_password_in_configmap(self):
        imports.add_source("other","192.0.2.11","root","fixture-password")
        secret=self.sent[0][2]
        self.assertTrue(secret["immutable"])
        self.assertNotEqual("homestead-src-other",secret["metadata"]["name"])
        row=json.loads(self.cm["data"]["sources.json"])[1]
        self.assertEqual(secret["metadata"]["name"],row["credential_secret"])
        self.assertNotIn("fixture-password",json.dumps(self.cm))
        self.assertNotIn("ssh_trust",row)

    def test_existing_name_cannot_overwrite_credentials(self):
        with self.assertRaises(ValueError):imports.add_source("tower","192.0.2.99","root","other")
        self.assertEqual([],self.sent)

    def test_inventory_failure_is_not_an_empty_source_list(self):
        with mock.patch.object(imports,"kget",side_effect=urllib.error.HTTPError("",503,"",{},None)):
            with self.assertRaises(urllib.error.HTTPError):imports.list_sources()

    def test_removing_source_retains_credentials_for_existing_helpers(self):
        imports.del_source("tower")
        self.assertEqual(["PUT"],[row[0] for row in self.sent])

    def test_trust_endpoints_require_admin(self):
        self.assertIn("/api/sources/scan",server.ADMIN_ROUTES)
        self.assertIn("/api/sources/trust",server.ADMIN_ROUTES)


class SourceProbeTests(unittest.TestCase):
    def run_probe(self,phase="Succeeded",authenticated=True,replaced=False):
        self.sent=[];metadata={}
        def send(method,path,body):
            self.sent.append((method,path,copy.deepcopy(body)))
            if method=="POST":
                metadata.update(body["metadata"],uid="probe-uid")
                return {"metadata":metadata}
        def get(path):return {"metadata":{**metadata,"uid":"replacement" if replaced else "probe-uid"},"status":{"phase":phase}}
        with mock.patch.object(imports,"ksend",side_effect=send),mock.patch.object(imports,"kget",side_effect=get), \
             mock.patch.object(imports.time,"sleep"),mock.patch.object(imports,"_pod_logs",return_value="result") as logs:
            try:
                return imports.run_probe("source","true",source(),authenticated=authenticated)
            finally:
                self.logs=logs

    def test_scan_pod_has_no_password_secret_or_serviceaccount_token(self):
        self.assertEqual(["result"],self.run_probe(authenticated=False))
        spec=self.sent[0][2]["spec"]
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertEqual([],spec["containers"][0]["env"])
        self.assertNotIn("Secret",json.dumps(spec))

    def test_authenticated_probe_has_pinned_key_and_secret_reference(self):
        self.run_probe()
        pod=self.sent[0][2];script=pod["spec"]["containers"][0]["command"][-1]
        self.assertIn(source()["ssh_trust"]["key"],script)
        self.assertEqual("SSHPASS",pod["spec"]["containers"][0]["env"][0]["name"])
        self.assertEqual("probe-uid",self.sent[-1][2]["preconditions"]["uid"])

    def test_failed_or_replaced_probe_never_returns_empty_success(self):
        for options in ({"phase":"Failed"},{"replaced":True}):
            with self.assertRaises(ValueError):self.run_probe(**options)
            self.logs.assert_not_called()
            self.assertEqual("probe-uid",self.sent[-1][2]["preconditions"]["uid"])
