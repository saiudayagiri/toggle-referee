#!/usr/bin/env python3
"""Reset the demo between takes: delete the demo flag (and with it its experiments).

After an experiment is completed, its flag serves the winner to everyone, so the
same flag cannot honestly host the same experiment again: "control" would no
longer be what users get. Deleting the flag puts the demo back at the start
without rebuilding Flagsmith. This is a demo convenience and deliberately not
a `referee` command.

    python3 demo/reset.py            # deletes the flag signup_form
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from referee import config  # noqa: E402
from referee.api import FlagsmithError  # noqa: E402


def main() -> int:
    flag = sys.argv[1] if len(sys.argv) > 1 else "signup_form"
    settings = config.load(ROOT)
    if settings.missing():
        print("Run ./demo/up.sh first.", file=sys.stderr)
        return 1
    client = settings.client()
    try:
        feature = client.feature_by_name(flag)
        if feature is None:
            print("There is no flag '%s'; nothing to reset." % flag)
            return 0
        client.request("DELETE", "/projects/%d/features/%d/" % (client.project_id, feature["id"]))
    except FlagsmithError as error:
        print("Could not reset: %s" % error, file=sys.stderr)
        return 1
    print("Deleted flag '%s' and its experiments. Run ./demo/walkthrough.sh for a fresh take." % flag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
