import os
import tempfile

# Tests drive the real CLI; never let them write training data into the user's store.
os.environ["AGENTHARNESS_DISTILL_DIR"] = tempfile.mkdtemp(prefix="nessa-distill-test-")
