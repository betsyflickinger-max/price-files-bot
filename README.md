# price-files-bot

Automatically collects and checks U.S. hospital and insurer price transparency files, and flags missing or broken ones.

## What it does (planned)

- **Hospital files (runs on GitHub, free):** finds each hospital's price file through its `cms-hpt.txt`, checks whether it changed without downloading it, downloads only changed files, saves originals to cloud storage and the Internet Archive, and flags broken or missing files.
- **Insurer files (monthly):** checks every insurer file for changes on GitHub, then starts rented machines only for the files that changed. The machines read the files, save results, and shut themselves down.
- **History:** every saved version records when a price changed.

## Data rules

Every price comes from a real source file and links back to it. Nothing is estimated or averaged. Each record shows when it was checked.

Change-detection ideas adapted from [hospital-price-history](https://github.com/lkowalcz/hospital-price-history) (CC0).
