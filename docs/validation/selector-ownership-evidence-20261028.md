# Service selector ownership evidence validation

The bounded Service investigation originally queried Pods through the Service selector. A Pod with a mismatching label was therefore invisible to that query. The follow-up now reads the uniquely scoped Deployment selector, discovers a bounded set of Pods through its simple match labels, and checks the observed Pod → ReplicaSet → Deployment controller UID chain. Every reported mismatch carries four resolvable observation references. The collector refuses unsupported selectors, incomplete inventories, changed object identities or versions, and insufficient citation capacity. A selector-only Deployment observation remains available as evidence without being mistaken for a conflicting full topology projection.

## Remote regression

The exact source snapshot passed 813 targeted tests and 2,558 complete tests in a network-isolated remote container. JavaScript syntax and the investigation rendering check also passed. Tests include changed resource versions, conflicting observations, multiple controller references, unsupported expressions, malformed falsey selector fields, citation limits, and the preservation of existing topology relationships.

## Isolated live experiment

A temporary, non-business Deployment and Service were created in a non-production namespace. Before injection, the Service endpoint referenced the temporary Pod UID. Changing a label on that Pod caused its Service endpoint set to become empty. Six bounded reads then yielded a mismatch for that same UID, a verified controller ownership chain, and four observation references resolvable in the evidence catalog. The temporary resources were removed and their absence verified; an independent watchdog and UID-preconditioned deletion were armed before creation.

The first server-side dry run refused a deadline field unsupported in a Deployment Pod template; no resources were created on that attempt. The experiment was repeated after removing that field while retaining the watchdog and cleanup controls.

The experiment supplied a synthetic pending hypothesis to exercise the collector. It did not run model diagnosis, affect business traffic, establish incident-time causation, execute remediation, or demonstrate autonomous recovery. Empty inventories do not refute a hypothesis, and these observations do not authorize an action. The diagnostic and self-healing closed loop therefore remains unaccepted by this experiment.
