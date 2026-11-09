"""Legal documents shared by ApollonIA and Querie (one VOP, one DPA, one
privacy policy, one sub-processor list), served at stable root URLs."""

# The addresses are quoted inside the documents themselves (VOP 11.3 and 15.4,
# DPA čl. 5 and Príloha č. 2, the privacy policy), so they must never move.
TERMS_URL = "/vop"
DPA_URL = "/zmluva-o-spracuvani-udajov"
PRIVACY_URL = "/ochrana-osobnych-udajov"
SUBPROCESSORS_URL = "/sprostredkovatelia"

# Effective date of the current versions. Recorded with every sign-up, so bump
# it (and the date shown in the documents) whenever a document changes.
VERSION = "2026-11-01"
