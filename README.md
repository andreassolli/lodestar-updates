# Item catalog (GitHub Actions)

Keeps `items.json`, a catalog of every AQW wiki item, up to date once a day.
The AQW Lodestar extension downloads it for its item lists.

## How it works

`scraper/update_catalog.py` reads the wiki's item listing newest edit first,
50 items per page. Each result already contains the item's page content and
tags, so no item pages are fetched. New or changed items are saved, and the run
stops after 25 unchanged items in a row. A normal day is one or two requests.

The first run reads the whole listing (around 950 pages, roughly half an hour).
If a run is cut short, by its time limit or an error, it records where it got
to and the next run carries on from there.

The workflow publishes the file to a separate `catalog` branch, one item per
line, so each day's commit is small and the data never ends up in the
extension package.

## Notes

- Items deleted from the wiki stay in the catalog. Delete `items.json` on the
  `catalog` branch to rebuild it from scratch.
- GitHub pauses scheduled workflows in repositories with no activity for
  60 days; re-enable it from the Actions tab if that happens.
# lodestar-updates
