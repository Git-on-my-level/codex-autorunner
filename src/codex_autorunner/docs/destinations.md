# Destinations

CAR can run repo work locally or in Docker-backed destinations.

Check a repo's effective destination:

`car hub destination show <repo_id> --path <hub_root>`

Set local execution:

`car hub destination set <repo_id> local --path <hub_root>`

Set Docker execution:

`car hub destination set <repo_id> docker --image <image> --path <hub_root>`

Use Docker when isolation or reproducible dependencies matter. Validate Docker daemon connectivity and image availability before long runs.
