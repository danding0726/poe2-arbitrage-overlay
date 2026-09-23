import sys


def self_test():
    import json
    from importlib.resources import files
    from rapidocr import RapidOCR
    from poe2arb.catalog import icon_file, item_name
    from poe2arb.core import find_candidates, historical_edges

    demo = json.loads(files("poe2arb").joinpath("demo.json").read_text(encoding="utf-8"))
    edges = historical_edges(demo["markets"], "演示联赛")
    base = "Metadata/Items/Currency/CurrencyAddModToRare"
    assert find_candidates(edges, base, lengths=(3,))
    assert item_name(base) == "崇高石"
    assert icon_file(base) is not None
    RapidOCR()


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        from poe2arb.overlay import run
        run()
