# Location is a Run parameter; the Session Pool lives inside a Run

"Multiple Sessions per Site" (#19) covered two needs: store/postcode selections that change the data, and parallel identities that only share the crawl. We split them. A **Location** is chosen per **Run** (one Run per Site and Location), and a **Session Pool** of interchangeable **Sessions** works inside a Run. We did not let one Run cover several Locations, because Run Health, Replay, `diff` and Sinks all assume a Run's Records describe one consistent context. Keeping one Location per Run leaves those unchanged and makes (Site, Location) the natural unit to schedule, for example as one Kubernetes Job each.

## Consequences

- The same Record Key in two Locations is two facts; `diff` refuses Runs of different Locations.
- Running every Location of a Site means launching one Run per Location, which is the scheduler's job, not the CLI's.
- A Session never changes what data is returned, so losing one only degrades a Run.
