# Installation

Note: for Technicolor plugin, the commonconf and the telconf files are taken
from the 'Binary' folder of the firmware zip file available on Technicolor
website.

The common configuration file is slightly modified from the original; do a
diff to see the difference.

The telconf file is not modified.

Note that if the common configuration file is not modified but the MAC
specific configuration file is modified, for example a parameter is removed,
than the phone WILL NOT take the value defined in the common configuration
file UNLESS the common configuration file name is changed. Yes. This has some
implication when you want to make a parameter takes its value defined in
the common configuration possible (since this behaviour is not possible).
