#!/usr/bin/env python3
import json, pathlib, subprocess, sys, tempfile, unittest

AUDIT=pathlib.Path(__file__).parents[1]/"tools"/"audit_build.py"

class AuditNegativeControls(unittest.TestCase):
    def run_case(self, relative: str, manifest: dict[str,str], expected: str) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root=pathlib.Path(raw); source=root/"source"; build=root/"build"
            (source/"llvm-project"/"llvm").mkdir(parents=True)
            (source/"llvm-project"/"llvm"/"CMakeLists.txt").write_text("# pinned\n")
            (source/"provenance").mkdir(); (source/"upstream"/"mimalloc-pprof-0.9.5").mkdir(parents=True)
            (source/"upstream"/"libxml2-2.15.3").mkdir(parents=True)
            (source/"provenance"/"mimalloc-pprof-files.json").write_text(json.dumps(manifest))
            (source/"provenance"/"libxml2-files.json").write_text("{}")
            candidate=source/relative; candidate.parent.mkdir(parents=True,exist_ok=True); candidate.write_text("int x;\n")
            (build/".cmake"/"api"/"v1"/"reply").mkdir(parents=True)
            (build/".cmake"/"api"/"v1"/"reply"/"codemodel-v2-test.json").write_text("{}")
            (build/"compile_commands.json").write_text(json.dumps([{"file":str(candidate),"directory":str(build)}]))
            inputs=build/"inputs.txt"; inputs.write_text("")
            result=subprocess.run([sys.executable,str(AUDIT),"--source",str(source),"--build",str(build),"--ninja-inputs",str(inputs),"--output",str(build/"out.json")],text=True,capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn(expected,result.stdout+result.stderr)

    def test_undeclared_wrapper_root_is_rejected(self):
        self.run_case("rogue.c",{},"undeclared compile input")

    def test_modified_mimalloc_input_is_rejected(self):
        self.run_case("upstream/mimalloc-pprof-0.9.5/bad.c",{"bad.c":"0"*64},"undeclared or modified mimalloc")

    def test_modified_libxml2_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root=pathlib.Path(raw); source=root/"source"; build=root/"build"
            (source/"llvm-project"/"llvm").mkdir(parents=True)
            (source/"llvm-project"/"llvm"/"CMakeLists.txt").write_text("# pinned\n")
            (source/"provenance").mkdir(); (source/"upstream"/"mimalloc-pprof-0.9.5").mkdir(parents=True)
            (source/"upstream"/"libxml2-2.15.3").mkdir(parents=True)
            (source/"provenance"/"mimalloc-pprof-files.json").write_text("{}")
            (source/"provenance"/"libxml2-files.json").write_text(json.dumps({"bad.c":"0"*64}))
            candidate=source/"upstream"/"libxml2-2.15.3"/"bad.c"
            candidate.parent.mkdir(parents=True,exist_ok=True); candidate.write_text("int x;\n")
            (build/".cmake"/"api"/"v1"/"reply").mkdir(parents=True)
            (build/".cmake"/"api"/"v1"/"reply"/"codemodel-v2-test.json").write_text("{}")
            (build/"compile_commands.json").write_text(json.dumps([{"file":str(candidate),"directory":str(build)}]))
            inputs=build/"inputs.txt"; inputs.write_text("")
            result=subprocess.run([sys.executable,str(AUDIT),"--source",str(source),"--build",str(build),"--ninja-inputs",str(inputs),"--output",str(build/"out.json")],text=True,capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn("undeclared or modified libxml2",result.stdout+result.stderr)

    def run_aux_case(self, channel: str, expected: str) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root=pathlib.Path(raw); source=root/"source"; build=root/"build"
            (source/"llvm-project"/"llvm").mkdir(parents=True)
            (source/"llvm-project"/"llvm"/"CMakeLists.txt").write_text("# pinned\n")
            (source/"provenance").mkdir(); (source/"upstream"/"mimalloc-pprof-0.9.5").mkdir(parents=True)
            (source/"upstream"/"libxml2-2.15.3").mkdir(parents=True)
            (source/"provenance"/"mimalloc-pprof-files.json").write_text("{}")
            (source/"provenance"/"libxml2-files.json").write_text("{}")
            declared=source/"src"/"declared.c"; declared.parent.mkdir(); declared.write_text("int x;\n")
            rogue=source/"rogue.c"; rogue.write_text("int rogue;\n")
            (build/".cmake"/"api"/"v1"/"reply").mkdir(parents=True)
            (build/".cmake"/"api"/"v1"/"reply"/"codemodel-v2-test.json").write_text("{}")
            (build/"compile_commands.json").write_text(json.dumps([{"file":str(declared),"directory":str(build)}]))
            inputs=build/"inputs.txt"; inputs.write_text("")
            command=[sys.executable,str(AUDIT),"--source",str(source),"--build",str(build),"--ninja-inputs",str(inputs),"--output",str(build/"out.json")]
            if channel == "deps":
                deps=build/"deps.txt"; deps.write_text(f"target: #deps 1, deps mtime 0 (VALID)\n    {rogue}\n")
                command += ["--ninja-deps",str(deps)]
            else:
                trace=build/"trace.jsonl"; trace.write_text(json.dumps({"file":str(rogue),"args":[]})+"\n")
                command += ["--cmake-trace",str(trace)]
            result=subprocess.run(command,text=True,capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn(expected,result.stdout+result.stderr)

    def run_vcs_case(self, relative: str) -> tuple[int,str,str]:
        with tempfile.TemporaryDirectory() as raw:
            root=pathlib.Path(raw); source=root/"source"; build=root/"build"
            (source/"llvm-project"/"llvm").mkdir(parents=True)
            (source/"llvm-project"/"llvm"/"CMakeLists.txt").write_text("# pinned\n")
            (source/"provenance").mkdir(); (source/"upstream"/"mimalloc-pprof-0.9.5").mkdir(parents=True)
            (source/"upstream"/"libxml2-2.15.3").mkdir(parents=True)
            (source/"provenance"/"mimalloc-pprof-files.json").write_text("{}")
            (source/"provenance"/"libxml2-files.json").write_text("{}")
            declared=source/"src"/"declared.c"; declared.parent.mkdir(); declared.write_text("int x;\n")
            vcs=source/relative; vcs.parent.mkdir(parents=True,exist_ok=True); vcs.write_text("ref: refs/heads/main\n")
            (build/".cmake"/"api"/"v1"/"reply").mkdir(parents=True)
            (build/".cmake"/"api"/"v1"/"reply"/"codemodel-v2-test.json").write_text("{}")
            (build/"compile_commands.json").write_text(json.dumps([{"file":str(declared),"directory":str(build)}]))
            inputs=build/"inputs.txt"; inputs.write_text(f"{vcs}\n")
            output=build/"out.json"
            result=subprocess.run([sys.executable,str(AUDIT),"--source",str(source),"--build",str(build),"--ninja-inputs",str(inputs),"--output",str(output)],text=True,capture_output=True)
            return result.returncode,result.stdout+result.stderr,(output.read_text() if output.is_file() else "")

    def test_vcs_metadata_read_is_exempt(self):
        code,log,inventory=self.run_vcs_case(".git/logs/HEAD")
        self.assertEqual(code,0,log)
        self.assertNotIn(".git",inventory)

    def test_source_shaped_file_under_vcs_dir_is_rejected(self):
        code,log,_=self.run_vcs_case(".git/evil.h")
        self.assertNotEqual(code,0)
        self.assertIn("undeclared build source read",log)

    def test_undeclared_ninja_dependency_is_rejected(self):
        self.run_aux_case("deps","undeclared dependency read")

    def test_undeclared_cmake_trace_is_rejected(self):
        self.run_aux_case("trace","undeclared CMake trace read")

if __name__ == "__main__": unittest.main()
