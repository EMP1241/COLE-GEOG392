"""Replay pinned previously audited candidates after matching fresh source rows."""

import hashlib
import json
import sqlite3

import shapely


def digest_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class VerifiedCandidates:
    def __init__(self, home, current_report, original_tree):
        self.home = home
        self.certified_prefix=False
        local=bool(current_report.get("conservative_local_exclusions_enabled"))
        self.run = home / ("working/national-repair-nj-v2-2026-10-21" if local else "working/national-repair-upper-snake-2026-10-21")
        self.database = self.run / "repair_audit.sqlite3"
        verification = home / ("verification/national-nj-continuation-audit.json" if local else "verification/national-upper-snake-continuation-audit.json")
        expected_db = "eb3420e454e866f7ebc5280f6687446b9013cd76c29dd007f614eed8f80bfeac" if local else "d1793aab8a9878796b4630e6b34cad30c0ae70b67cab8b39da213114880fa033"
        expected_check = "10d176cbef8958101b22df21b163a3ac5505a6f5c3f0b4df1a1bf85a45f4f789" if local else "f59da5c7b77c1c240e7e661d380c6c052f4c633930ef36972ef030c101586de3"
        expected_count=789 if local else 593
        self.allowed_stop=("PADUS4_1Fee",77431 if local else 41938)
        if current_report.get("eastern_canal_adjudication_enabled"):
            if not local:
                raise RuntimeError("Eastern Canal continuation requires county review mode")
            self.run = home / "working/national-prep-local-exclusions-2026-10-22"
            self.database = self.run / "repair_audit.sqlite3"
            verification = home / "verification/national-local-exclusions-audit-v2-2026-10-22.json"
            expected_db = "76f8ae2050a030f5e4208dd28ef94cf5e77330f1c84f2095654ee2b0123d8f81"
            expected_check = "dd57506d7ab6d3574b3e2283f9d6fb80a13c4ab15a3734c368a65cebdf4b8f4e"
            expected_count = 850
            self.allowed_stop = ("PADUS4_1Fee", 88623)
        if current_report.get("batch_county_review_enabled"):
            self.run=home/"working/national-prep-eastern-canal-2026-10-22"
            self.database=self.run/"repair_audit.sqlite3"
            verification=home/"verification/national-eastern-canal-continuation-audit-2026-10-22.json"
            expected_db="8be0006ae997706efd15682646a949c2cd13afe9a86c4a94fa713f36034db178"
            expected_check="01537bea51444d56f7c195e43a27f39eb0cefb5787e23fe663b73eb01f7aabc5"
            expected_count=1181
            self.allowed_stop=("PADUS4_1Fee",199053)
        resume=current_report.get("certified_batch_resume")
        if resume:
            verification=(home/resume["audit"]).resolve()
            if not verification.is_relative_to((home/"verification").resolve()) or digest_file(verification)!=resume["sha256"]:
                raise RuntimeError("Certified prefix audit path/hash differs")
            certificate=json.loads(verification.read_text())
            if certificate["status"]!="passed" or certificate["discrepancies"]:
                raise RuntimeError("Do not reuse a failed or unfinished batch audit")
            self.run=(home/certificate["run"]).resolve()
            if not self.run.is_relative_to((home/"working").resolve()):
                raise RuntimeError("Certified prefix run escapes feature working directory")
            self.database=self.run/"repair_audit.sqlite3"
            expected_db=certificate["audit_database"]["sha256"]
            expected_check=resume["sha256"]
            expected_count=certificate["repairs_checked"]
            self.allowed_stop=None
            self.certified_prefix=True
        if digest_file(self.database) != expected_db or digest_file(verification) != expected_check:
            raise RuntimeError("Prior verified-candidate database or audit changed")
        prior = json.loads((self.run / "repair-summary.json").read_text(encoding="utf-8"))
        check = json.loads(verification.read_text(encoding="utf-8"))
        if current_report.get("eastern_canal_adjudication_enabled") and (
                check.get("county_allocations_recomputed_from_preserved_census") is not True
                or digest_file(self.run / "preparation-policy.json") != check["preparation_policy"]["sha256"]):
            raise RuntimeError("Prior county preparation policy or allocation audit differs")
        if (check["status"] != "passed" or check["discrepancies"] or prior["repairs_logged"] != expected_count
                or prior["source_archive"] != current_report["source_archive"]
                or prior["python_dependencies"] != current_report["python_dependencies"]
                or prior["thresholds"] != current_report["thresholds"] or prior["method"] != current_report["method"]
                or (not self.certified_prefix and prior["stop_record"]["source_fid"] != self.allowed_stop[1])
                or (self.certified_prefix and (prior.get("batch_county_review_enabled") is not True or prior["stop_record"] is not None))
                or json.loads((self.run / "source-extraction-before.json").read_text(encoding="utf-8")) != original_tree):
            raise RuntimeError("Prior candidate source/method/engine/preservation evidence differs")
        self.connection = sqlite3.connect(self.database.resolve().as_uri() + "?mode=ro", uri=True)
        self.connection.row_factory = sqlite3.Row
        self.keys = {tuple(row) for row in self.connection.execute("SELECT layer,source_fid FROM repairs")}
        if len(self.keys) != expected_count:
            raise RuntimeError("Prior candidate identities differ")
        self.used = set()
        self.description = {"run":self.run.relative_to(home).as_posix(),"database_sha256":expected_db,
                            "audit_sha256":expected_check,"available_candidates":expected_count,
                            "scope":"Raw candidates only; fresh original WKB/attributes matched; all review proofs and specific replacements rechecked"}

    def get(self, layer, fid, original, attributes):
        key = (layer, fid)
        if key not in self.keys:
            return None
        if key in self.used:
            raise RuntimeError("Previously verified candidate replayed twice")
        row = self.connection.execute("SELECT * FROM repairs WHERE layer=? AND source_fid=?", key).fetchone()
        body = row["original_wkb"]
        if (hashlib.sha256(body).hexdigest() != row["original_sha256"]
                or hashlib.sha256(shapely.to_wkb(original,byte_order=1)).hexdigest() != row["original_sha256"]
                or json.loads(row["attributes_json"]) != attributes):
            raise RuntimeError(f"Fresh source differs from verified candidate {key}")
        # The prior record's accepted derivative is not the raw linework input.
        special = self.connection.execute("SELECT previous_candidate_wkb FROM accepted_adjudications WHERE layer=? AND source_fid=?", key).fetchone()
        if special is None and self.allowed_stop in {("PADUS4_1Fee", 88623),("PADUS4_1Fee",199053)}:
            special = self.connection.execute("SELECT previous_candidate_wkb FROM conservative_local_exclusions WHERE layer=? AND source_fid=?", key).fetchone()
        if special is None and self.certified_prefix:
            special=self.connection.execute("SELECT previous_candidate_wkb FROM conservative_local_exclusions WHERE layer=? AND source_fid=?",key).fetchone()
            if special is None:
                special=self.connection.execute("SELECT previous_candidate_wkb FROM batch_reconciliations WHERE layer=? AND source_fid=?",key).fetchone()
        candidate = special[0] if special else row["repaired_wkb"]
        if not special and hashlib.sha256(candidate).hexdigest() != row["repaired_sha256"]:
            raise RuntimeError("Saved raw candidate WKB hash differs")
        if json.loads(row["stop_reasons_json"]) and key != self.allowed_stop:
            raise RuntimeError("Unresolved prior stop cannot be replayed")
        self.used.add(key)
        return shapely.from_wkb(candidate), {"source_run":self.description["run"],
                "source_database_sha256":self.description["database_sha256"],
                "original_wkb_sha256":row["original_sha256"],
                "raw_candidate_wkb_sha256":hashlib.sha256(candidate).hexdigest()}

    def batch_certificate(self,layer,fid):
        if not self.certified_prefix:
            return None
        key=(layer,fid)
        if key not in self.used:
            raise RuntimeError("Match fresh source before reusing a batch certificate")
        row=self.connection.execute("SELECT b.proof_json,b.independent_wkb,r.polygon_sha256,r.stop_reasons_json FROM batch_reconciliations b JOIN repairs r USING(layer,source_fid) WHERE layer=? AND source_fid=?",key).fetchone()
        if row is None:
            return None
        if json.loads(row[3]) or hashlib.sha256(row[1]).hexdigest()!=row[2]:
            raise RuntimeError("Unresolved or inconsistent prefix batch certificate")
        return shapely.from_wkb(row[1]),json.loads(row[0])

    def close(self):
        self.connection.close()
        if digest_file(self.database) != self.description["database_sha256"]:
            raise RuntimeError("Prior candidate audit changed during replay")
