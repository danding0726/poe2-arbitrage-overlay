import sys


def self_test():
    from rapidocr import RapidOCR
    from poe2arb.catalog import icon_file, item_name
    from poe2arb.single_item import DIVINE, EXALTED, Quote, evaluate

    item = "Metadata/Items/Currency/CurrencyCorrupt"
    quotes = (
        Quote(EXALTED, item, 30, 2, 10, 100, 100),
        Quote(item, DIVINE, 2, 1, 4, 101, 200),
        Quote(DIVINE, EXALTED, 1, 35, 200, 102, 300),
    )
    assert evaluate(*quotes, now=110).profit == 5
    assert item_name(EXALTED) == "崇高石"
    assert icon_file(EXALTED) is not None
    RapidOCR()


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        from poe2arb.dashboard import run
        run()
