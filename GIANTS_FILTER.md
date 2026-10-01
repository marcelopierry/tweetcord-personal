# New York Giants channel filter

The filter applies only to channel `1543702875954483310` (`new-york-giants`).
Posts tracked from the official `@Giants` account are exempt, including its reposts.
Other channels and their delivery history are unaffected.

Before claiming delivery history or sending links/cards/media, the bot checks the
fresh, validated tweet text and its quoted original. Retweets use the original
text. Any allowed Giants term wins over every blocked term, regardless of order.
Otherwise, the seven requested team names, their roster players' full names,
first names, surnames, and configured nicknames block the entire post.
Matching ignores case/accents and uses whole words/phrases, including hashtags.
URL targets are excluded; visible link labels are included.

Rules are stored in `src/notification/giants_filter_rules.json`. Rosters were
retrieved on October 1, 2026 from the MLB and ESPN roster APIs listed in that file.
The MLB lists include the 40-man roster. Nicknames are an explicit alias list;
new nicknames, trades and roster additions require updating this file and
restarting the service. No runtime roster lookup or paid API is needed.

First names are intentionally broad: for example, an unrelated mention of Aaron
can be filtered unless a Giants allow term appears. Shared names obey the same
allow-first rule. Allowed terms cover Giants/NYG/NFL, Harbaugh and key Giants
players, including Dart, Nabers, Carter, Burns, Thibodeaux and Skattebo.

Filtered posts are logged with the matched term, but are not marked delivered.
