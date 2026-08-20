from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = Path(
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
)


def run_script(path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PSModulePath"] = os.pathsep.join(
        item
        for item in environment.get("PSModulePath", "").split(os.pathsep)
        if r"\powershell\7\modules" not in item.lower()
    )
    return subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(path),
            *arguments,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=environment,
    )


class QwenProfileTests(unittest.TestCase):
    scripts = (
        ROOT / "windows" / "Start-ExcaliburQwen38.ps1",
        ROOT / "windows" / "Install-ExcaliburQwen38.ps1",
        ROOT / "windows" / "Switch-ExcaliburBackend.ps1",
        ROOT / "bin" / "doctor.ps1",
    )

    def test_all_lifecycle_scripts_parse_in_windows_powershell(self) -> None:
        joined = ",".join(f"'{path}'" for path in self.scripts)
        command = (
            f"$failed=@(); foreach($path in @({joined})) "
            "{$tokens=$null;$errors=$null;"
            "[System.Management.Automation.Language.Parser]::ParseFile("
            "$path,[ref]$tokens,[ref]$errors)|Out-Null;"
            "if(@($errors).Count){$failed += $errors.Message}};"
            "if($failed.Count){$failed -join '; ';exit 1}"
        )
        result = subprocess.run(
            [str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env={
                **os.environ,
                "PSModulePath": os.pathsep.join(
                    item
                    for item in os.environ.get("PSModulePath", "").split(os.pathsep)
                    if r"\powershell\7\modules" not in item.lower()
                ),
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_runtime_profiles_are_exact_and_share_one_implementation(self) -> None:
        script = ROOT / "windows" / "Start-ExcaliburQwen38.ps1"
        records = {}
        for profile in ("Bounded", "Native"):
            result = run_script(script, "-Profile", profile, "-ValidateOnly")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            records[profile] = json.loads(result.stdout)

        bounded = records["Bounded"]
        self.assertEqual(bounded["task"], "Coding Intelligence Excalibur Qwen3.8")
        self.assertEqual(bounded["modelAlias"], "arm-qwen38-q6-text")
        self.assertEqual(bounded["contextTokens"], 32768)
        self.assertEqual(bounded["cacheType"], "q8_0")
        self.assertTrue(bounded["mtp"])

        native = records["Native"]
        self.assertEqual(native["task"], "Coding Intelligence Excalibur Qwen3.8 Native")
        self.assertEqual(native["modelAlias"], "arm-qwen38-q6-native-262k")
        self.assertEqual(native["contextTokens"], 262144)
        self.assertEqual(native["cacheType"], "q4_0")
        self.assertFalse(native["mtp"])
        self.assertEqual(native["reasoningEffort"], "medium")
        self.assertLessEqual(native["reasoningBudget"], 24576)
        self.assertNotEqual(native["stateRoot"], bounded["stateRoot"])
        self.assertEqual(native["script"], bounded["script"])

    def test_installers_validate_both_on_demand_profiles_without_mutation(self) -> None:
        script = ROOT / "windows" / "Install-ExcaliburQwen38.ps1"
        for profile in ("Bounded", "Native"):
            with self.subTest(profile=profile):
                result = run_script(script, "-Profile", profile, "-ValidateOnly")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                record = json.loads(result.stdout)
                self.assertEqual(record["profileName"], profile)
                self.assertTrue(record["onDemand"])
                self.assertEqual(record["triggerCount"], 0)

    def test_switcher_has_three_way_mutual_exclusion_and_exact_rollback(self) -> None:
        text = (ROOT / "windows" / "Switch-ExcaliburBackend.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn('ValidateSet("Qwen38", "Qwen38Native", "Ollama")', text)
        self.assertIn("Assert-OtherBackendsStopped", text)
        self.assertIn("exact previous backend", text)
        self.assertIn("mutualExclusionProven = $true", text)
        self.assertIn("$ownedProcessIds = @(\n", text)
        self.assertNotIn("Stop-Process -Name", text)

    def test_doctor_names_all_three_backends_and_both_profiles(self) -> None:
        text = (ROOT / "bin" / "doctor.ps1").read_text(encoding="utf-8")
        for expected in (
            "qwen38Native",
            "Qwen38 Native",
            "q6-text/medium/q8_0/32768/mtp3",
            "q6-text/medium/q4_0/262144/mtp-off",
            "AnimeFrontier Excalibur Ollama",
        ):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
