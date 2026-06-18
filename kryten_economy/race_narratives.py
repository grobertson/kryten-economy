"""Static narrative pools for race commentary.

All strings support ``str.format()`` placeholders:
- ``{racer}``  — name/colour of a racer
- ``{emoji}``  — emoji of that racer
- ``{user}``   — a bettor's username
- ``{payout}`` — winning amount
- ``{symbol}`` — currency symbol
"""

from __future__ import annotations

# ── Race start announcements ─────────────────────────────────

START_LINES: tuple[str, ...] = (
    "🏁 The racers are lined up and the crowd is going wild!",
    "🏁 Engines revving! The starting gate is about to drop!",
    "🏁 The track is set, the bets are in — let's see who's got the speed!",
    "🏁 Racers at the ready! This one's gonna be a barnburner!",
    "🏁 The stadium lights are blazing — it's race time!",
    "🏁 Dust clouds forming at the starting line — here we go!",
    "🏁 The announcer grabs the mic: 'Ladies and gentlemen... START YOUR ENGINES!'",
    "🏁 The crowd falls silent... then ERUPTS as the flag drops!",
    "🏁 A horn blasts across the arena — the race is ON!",
    "🏁 The ground shakes as the racers launch from the gate!",
)

# ── Lead change commentary ───────────────────────────────────

LEAD_CHANGE_LINES: tuple[str, ...] = (
    "{emoji} {racer} surges into the lead!",
    "{emoji} {racer} muscles past and takes first place!",
    "What a move! {emoji} {racer} is now in front!",
    "{emoji} {racer} finds another gear and TAKES THE LEAD!",
    "The crowd roars — {emoji} {racer} storms to the front!",
    "OUT OF NOWHERE! {emoji} {racer} blasts into first!",
    "{emoji} {racer} makes a daring move and seizes the lead!",
    "Look at {emoji} {racer} go! New leader on the track!",
    "Position swap! {emoji} {racer} takes command!",
    "{emoji} {racer} threads the needle and grabs P1!",
)

# ── Close finish commentary ──────────────────────────────────

CLOSE_FINISH_LINES: tuple[str, ...] = (
    "It's neck and neck! This could go either way!",
    "I can't tell them apart — this is INSANELY close!",
    "Photo finish incoming! The crowd is on their feet!",
    "They're shoulder to shoulder heading into the final stretch!",
    "THREE racers within spitting distance of the finish line!",
    "This is the closest race we've seen all day!",
    "The gap is paper thin — who WANTS it more?!",
    "Every pixel matters now — this is going down to the wire!",
)

# ── Random event commentary ──────────────────────────────────

EVENT_LINES: dict[str, tuple[str, ...]] = {
    "speed_boost": (
        "⚡ {emoji} {racer} hits a speed boost! ZOOM!",
        "⚡ {emoji} {racer} activates the afterburners!",
        "⚡ Turbo engaged! {emoji} {racer} rockets forward!",
        "⚡ {emoji} {racer} found a nitro canister on the track!",
    ),
    "stumble": (
        "💥 {emoji} {racer} stumbles and loses ground!",
        "💥 {emoji} {racer} hits a pothole and wobbles!",
        "💥 Oh no! {emoji} {racer} trips up!",
        "💥 {emoji} {racer} catches a bad patch and slows down!",
    ),
    "mudslide": (
        "🌊 MUDSLIDE on the track! Everyone's slowing down!",
        "🌊 The track floods! All racers are wading through mud!",
        "🌊 A burst pipe soaks the course — everyone's affected!",
    ),
    "shortcut": (
        "🎯 {emoji} {racer} spots a shortcut and darts through!",
        "🎯 The trailing {emoji} {racer} finds a gap in the fence!",
        "🎯 {emoji} {racer} takes a daring detour and gains ground!",
    ),
}

# ── Finish line / winner ─────────────────────────────────────

FINISH_LINES: tuple[str, ...] = (
    "🏆 {emoji} {racer} WINS THE RACE!",
    "🏆 {emoji} {racer} crosses the finish line first! INCREDIBLE!",
    "🏆 AND THE WINNER IS... {emoji} {racer}!",
    "🏆 {emoji} {racer} takes the chequered flag!",
    "🏆 IT'S {emoji} {racer} BY A NOSE! WHAT A RACE!",
    "🏆 {emoji} {racer} claims victory! The crowd ERUPTS!",
    "🏆 The dust settles and {emoji} {racer} stands TRIUMPHANT!",
    "🏆 {emoji} {racer} BLAZES across the line — WINNER!",
)

# ── Payout announcement ──────────────────────────────────────

PAYOUT_LINES: tuple[str, ...] = (
    "💰 {user} cashes in big — +{payout} {symbol}!",
    "💰 Payday for {user}! +{payout} {symbol} in the bank!",
    "💰 {user} collects {payout} {symbol}! Smart bet!",
    "💰 Cha-ching! {user} walks away with +{payout} {symbol}!",
    "💰 {user} picked the winner — +{payout} {symbol}!",
)

# ── Racer trait descriptions ─────────────────────────────────

TRAIT_DESCRIPTIONS: dict[str, str] = {
    "sprinter": "⚡ Fast start",
    "steady": "🎯 Consistent",
    "closer": "🔥 Late surge",
    "wildcard": "🎲 High variance",
    "resilient": "🛡️ Event-proof",
}
