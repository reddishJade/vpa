# version-promotion-agent

## Developer

Run the local dry-run fixture test to verify VPA end-to-end with mock agent:

    uv run pytest vpa/tests/test_dry_run.py -v

The consolidated test fixtures are in `vpa/tests/fixtures.py`. Import `MockAgent`,
`MockValidation`, and repo builders from there for new tests.
