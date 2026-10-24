import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sas_lineage_lite as L

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "retail_loans.sas"


def edges(code):
    return L.parse_text(code).edges


def pairs(code, target):
    return {(e.source_table, e.source_field) for e in edges(code)
            if f"{e.target_table}.{e.target_field}" == target}


class DataStepTests(unittest.TestCase):
    def test_assignment_sources(self):
        code = "data out; set lib.a; z = x + y; run;"
        self.assertEqual(pairs(code, "WORK.OUT.Z"), {("LIB.A", "X"), ("LIB.A", "Y")})

    def test_bare_name_is_work(self):
        self.assertEqual(pairs("data b; set a; c = d; run;", "WORK.B.C"), {("WORK.A", "D")})

    def test_macro_variable_substituted(self):
        code = "%let t = lib.src; data out; set &t; k = v; run;"
        self.assertEqual(pairs(code, "WORK.OUT.K"), {("LIB.SRC", "V")})

    def test_rename_option_points_to_old_name(self):
        code = "data o; set l.a (rename=(old=new)); z = new * 2; run;"
        self.assertEqual(pairs(code, "WORK.O.Z"), {("L.A", "OLD")})

    def test_if_then_else_is_conditional(self):
        code = "data o; set l.a; if s > 1 then g='H'; else g='L'; run;"
        kinds = {e.kind for e in edges(code) if e.target_field == "G"}
        self.assertEqual(kinds, {"CONDITIONAL"})
        self.assertEqual(pairs(code, "WORK.O.G"), {("L.A", "S")})

    def test_string_and_date_literals_are_not_columns(self):
        code = "data o; set l.a; z = catx('-', p, 'abc'); d = intck('year', b, '01jan2020'd); run;"
        self.assertEqual(pairs(code, "WORK.O.Z"), {("L.A", "P")})
        self.assertEqual(pairs(code, "WORK.O.D"), {("L.A", "B")})

    def test_two_inputs_are_flagged_not_guessed(self):
        res = L.parse_text("data o; merge l.a l.b; by id; z = x; run;")
        e = [e for e in res.edges if e.target_field == "Z"][0]
        self.assertEqual(e.source_table, "?")
        self.assertTrue(e.note)

    def test_keep_creates_direct_edges(self):
        code = "data o; set l.a; keep id amt; run;"
        self.assertEqual(pairs(code, "WORK.O.ID"), {("L.A", "ID")})


class SqlTests(unittest.TestCase):
    def test_alias_resolved(self):
        code = "proc sql; create table o as select a.x as y from l.t a; quit;"
        self.assertEqual(pairs(code, "WORK.O.Y"), {("L.T", "X")})

    def test_join_two_tables(self):
        code = ("proc sql; create table o as select a.x, b.w as w2 "
                "from l.t a left join l.u b on a.id = b.id; quit;")
        self.assertEqual(pairs(code, "WORK.O.X"), {("L.T", "X")})
        self.assertEqual(pairs(code, "WORK.O.W2"), {("L.U", "W")})

    def test_aggregate_kind(self):
        code = "proc sql; create table o as select sum(a.x) as tot from l.t a group by a.g; quit;"
        e = [e for e in edges(code) if e.target_field == "TOT"][0]
        self.assertEqual(e.kind, "AGGREGATE")


class ReportingTests(unittest.TestCase):
    def test_example_has_no_unresolved_sources(self):
        res = L.parse_files([str(EXAMPLE)])
        self.assertEqual([e for e in res.edges if e.source_table == "?"], [])
        self.assertEqual(res.warnings, [])

    def test_trace_reaches_raw_inputs(self):
        res = L.parse_files([str(EXAMPLE)])
        chains = L.trace(res.edges, "MART.REGION_REPORT.COST_RATIO")
        starts = {c[0] for c in chains}
        self.assertIn("RAW.LOAN_EXTRACT.BAL", starts)
        self.assertIn("RAW.LOAN_EXTRACT.RATE", starts)

    def test_mermaid_mentions_tables(self):
        res = L.parse_files([str(EXAMPLE)])
        text = L.mermaid(res.edges)
        self.assertTrue(text.startswith("flowchart"))
        self.assertIn("MART.REGION_REPORT", text)

    def test_unsupported_macro_warns(self):
        res = L.parse_text("%macro m; data o; set a; x=y; run; %mend; %m;")
        self.assertTrue(res.warnings)


if __name__ == "__main__":
    unittest.main()
