import unittest

from poe2arb.catalog import catalog, icon_file, item_name


class CatalogTests(unittest.TestCase):
    def test_known_currency_has_local_icon_and_name(self):
        exalt = "Metadata/Items/Currency/CurrencyAddModToRare"
        vaal = "Metadata/Items/Currency/CurrencyCorrupt"
        self.assertEqual(item_name(exalt), "崇高石")
        self.assertEqual(item_name(vaal), "瓦尔宝珠")
        self.assertTrue(icon_file(exalt).is_file())
        self.assertTrue(icon_file(vaal).is_file())

    def test_unknown_id_gets_readable_fallback(self):
        self.assertEqual(item_name("Metadata/Items/Currency/SomeNewOrb5"), "Some New Orb 5")
        self.assertIsNone(icon_file("Metadata/Items/Currency/SomeNewOrb5"))

    def test_complete_exchange_catalog_is_bundled(self):
        self.assertGreaterEqual(len(catalog()), 679)
        with_icons = sum(icon_file(item_id) is not None for item_id in catalog())
        self.assertGreaterEqual(with_icons, 670)


if __name__ == "__main__":
    unittest.main()
