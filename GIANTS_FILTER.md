# New York Giants channel filter

The filter applies only to channel `1543702875954483310` (`new-york-giants`).
Posts tracked from the official `@Giants` account are exempt, including its reposts.
Other channels and their delivery history are unaffected.

Before claiming delivery history or sending links/cards/media, the bot checks the
fresh, validated tweet text and its quoted original. Retweets use the original
text. Any allowed Giants term wins over every blocked term, regardless of order.
Otherwise, the seven requested team names and their roster players' full names
block the entire post. Bare first names never block. Surnames and configured
nicknames only block alongside clear context for that player's sport: for
example, "Judge hit a homer" or "Cole pitched six innings", but not "Aaron
made a great throw", "Rice is back", or "OG is back". Generic baseball context
without a recognized player surname/nickname is allowed.
Names/nicknames that also form ordinary words or phrases (Judge, Rice, Bridges,
Glass, OG, All Rise, and others) additionally require an immediately attached
athlete action. "Judge just homered" is evidence; "I'll be the judge of this
baseball game" is not. Whole-word matching does not confuse judge with judged
or judgment. Commentary and quoted text are evaluated separately for blocking
evidence, so they cannot accidentally form a name/action across the boundary.
Football terms such as football, quarterback, touchdown, wide receiver and QB
also override every block, including explicit team names and full player names.
Matching ignores case/accents and uses whole words/phrases, including hashtags.
URL targets are excluded; visible link labels are included.

Rules are stored in `src/notification/giants_filter_rules.json`. Rosters were
retrieved on October 1, 2026 from the MLB and ESPN roster APIs listed in that file.
The MLB lists include the 40-man roster. Nicknames are an explicit alias list;
new nicknames, trades and roster additions require updating this file and
restarting the service. No runtime roster lookup or paid API is needed.

The filter deliberately favors letting uncertain posts through. Allowed terms
cover Giants/NYG/NFL, general football context, Harbaugh and key Giants
players, including Dart, Nabers, Carter, Burns, Thibodeaux and Skattebo.

Filtered posts are logged with the matched term, but are not marked delivered.
