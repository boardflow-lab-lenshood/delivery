# Boardflow real integration lab

Three services imported from Docker's Apache-2.0 Example Voting App:

- [vote](https://github.com/boardflow-lab-lenshood/vote): Python/Flask → Redis
- [worker](https://github.com/boardflow-lab-lenshood/worker): .NET → PostgreSQL
- [result](https://github.com/boardflow-lab-lenshood/result): Node.js/Socket.IO

Upstream: https://github.com/dockersamples/example-voting-app at
`63e9150ca17af4ed05880d4245e486481f73fcb4`. Original LICENSE is included.

This repository holds a real GitHub Actions workflow for Boardflow UAT.
It runs only by explicit workflow_dispatch, on the dedicated repository runner.
Each operation uses separate Docker containers, network and ephemeral data;
test and simulated production environments are isolated. No real production
system or personal information is involved.

The workflow accepts operation_id and delivery_target. It publishes the
immutable artifact `board-delivery-observation-<operation_id>`, containing
observation.json. A green workflow alone is insufficient to prove deployment.

Feature branches and PRs belong in the service repositories. Never commit
access tokens, model credentials, runner registration files or database secrets.

For `SETTLE`, `real-uat-test` removes only resources owned by
`settles_operation`. Production requires `retain: true`, the original
`settles_operation` and `settles_run`; it checks the original images and live
health without removing containers or networks. Missing or invalid production
retention is rejected. Never infer retention from a green workflow alone.

The workflow downloads this public repository at the exact `github.sha` using
GitHub's archive endpoint into a fresh directory. It runs the resource retention
tests before any deployment work. Run those tests locally with:

```sh
python3 -m unittest discover -s ci -p 'test_*.py'
```
