from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = (
    ROOT
    / "skills"
    / "mars-research-assistant"
    / "skills"
    / "deep-equity-research"
    / "reference"
    / "industry_registry.json"
)

ALLOWED_VALUATION_METHODS = {
    "dcf",
    "reverse_dcf",
    "pvgo",
    "pe",
    "epv",
    "eva",
    "sotp",
    "monte_carlo",
}

EXPECTED_INDUSTRIES = {
    "semiconductors": "半导体",
    "software_internet": "软件与互联网",
    "consumer_electronics": "消费电子",
    "ecommerce_retail": "电商与零售",
    "banks": "银行",
    "insurance": "保险",
    "brokers_exchanges": "券商与交易所",
    "pharma_biotech": "医药与生物科技",
    "medical_devices": "医疗器械",
    "consumer_staples": "消费必需品",
    "auto_parts": "汽车与零部件",
    "energy_utilities": "能源与公用事业",
    "industrials": "工业与制造",
    "real_estate_reits": "房地产与REITs",
    "telecom_media": "电信与传媒",
    "transport_logistics": "交通运输与物流",
}

FINANCIAL_INDUSTRIES = {"banks", "insurance", "brokers_exchanges"}

SIX_DIMENSIONS = (
    "key_kpis",
    "history_fields",
    "forecast_drivers",
    "valuation_methods",
    "counter_evidence",
    "min_data",
)


def load_registry() -> dict:
    with REGISTRY_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


class IndustryRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_registry()
        cls.industries = cls.registry["industries"]
        cls.by_id = {item["id"]: item for item in cls.industries}

    def test_registry_metadata(self) -> None:
        self.assertEqual(self.registry.get("schema_version"), 1)
        self.assertTrue(self.registry.get("registry_version"))

    def test_exactly_sixteen_industries_matching_contract(self) -> None:
        self.assertEqual(len(self.industries), 16)
        ids = [item["id"] for item in self.industries]
        self.assertEqual(len(ids), len(set(ids)), "industry id must be unique")
        self.assertEqual(set(ids), set(EXPECTED_INDUSTRIES))
        for industry_id, name_zh in EXPECTED_INDUSTRIES.items():
            self.assertEqual(self.by_id[industry_id]["name_zh"], name_zh)

    def test_six_dimensions_non_empty(self) -> None:
        for item in self.industries:
            with self.subTest(industry=item["id"]):
                for dimension in SIX_DIMENSIONS:
                    self.assertIn(dimension, item)
                    self.assertTrue(item[dimension], f"{dimension} empty")
                self.assertGreaterEqual(len(item["key_kpis"]), 4)
                self.assertLessEqual(len(item["key_kpis"]), 8)
                self.assertGreaterEqual(len(item["counter_evidence"]), 3)
                self.assertLessEqual(len(item["counter_evidence"]), 6)

    def test_valuation_methods_subset_of_allowed(self) -> None:
        for item in self.industries:
            with self.subTest(industry=item["id"]):
                self.assertTrue(
                    set(item["valuation_methods"]) <= ALLOWED_VALUATION_METHODS
                )

    def test_financial_industries_exclude_dcf_and_pvgo(self) -> None:
        for industry_id in FINANCIAL_INDUSTRIES:
            with self.subTest(industry=industry_id):
                methods = set(self.by_id[industry_id]["valuation_methods"])
                self.assertNotIn("dcf", methods)
                self.assertNotIn("pvgo", methods)
                self.assertTrue({"epv", "eva"} & methods)

    def test_at_least_three_industries_support_sotp(self) -> None:
        sotp_ids = [
            item["id"]
            for item in self.industries
            if "sotp" in item["valuation_methods"]
        ]
        self.assertGreaterEqual(len(sotp_ids), 3)

    def test_industries_are_differentiated(self) -> None:
        items = list(self.industries)
        for index, left in enumerate(items):
            for right in items[index + 1 :]:
                pair = (left["id"], right["id"])
                same_kpis = left["key_kpis"] == right["key_kpis"]
                same_counter = left["counter_evidence"] == right["counter_evidence"]
                self.assertFalse(
                    same_kpis and same_counter,
                    f"{pair} share identical key_kpis and counter_evidence",
                )

    def test_min_data_requirements(self) -> None:
        for item in self.industries:
            with self.subTest(industry=item["id"]):
                min_data = item["min_data"]
                self.assertGreaterEqual(min_data["annual_years"], 3)
                self.assertGreaterEqual(min_data["quarters"], 8)
                self.assertTrue(min_data["required_fields"])


if __name__ == "__main__":
    unittest.main()
