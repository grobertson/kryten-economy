"""Heist narrative template library.

Massive collection of dramatic heist text templates organised by category.
Each template uses ``str.format()`` placeholders:

- **Scenarios**: ``{user}`` — random crew member name
- **Win lines**: ``{payout}``, ``{symbol}``, ``{user}``
- **Lose lines**: ``{user}``, ``{symbol}``
- **Push lines**: ``{user}``, ``{symbol}``
- **Join lines**: ``{user}``
"""

from __future__ import annotations

# ══════════════════════════════════════════════════════════════
#  Scenarios — the premise shown when heist resolves
#  ~50 entries across many themes
# ══════════════════════════════════════════════════════════════

SCENARIOS: list[str] = [
    # ── Bank ──
    "🏦 Your team enters the casino. {user} disables the cameras while the rest get to work…",
    "🏦 As the bank doors swing open, {user} yells, \"EVERYONE ON THE FLOOR!!!\" 💰",
    "🏦 The crew rolls up to the armored truck in a black sedan. {user} pulls out the acetylene torch… 🔥",
    "🏦 Under cover of night, the crew slides down ropes into the vault. {user} whispers: \"Nobody make a sound…\" 🤫",
    "🏦 {user} hacks the security mainframe — \"We've got 90 seconds before the grid resets!\" 💻🔓",
    "🏦 The tunnel breaks through the floor of the vault. Gold bars everywhere. {user} grins: \"Jackpot.\" 🪙",
    "🏦 {user} poses as a bank inspector while the crew sneaks in through the back loading dock. Clipboards can open any door. 📋",
    "🏦 The ceiling panel drops open. {user} descends on a rappelling harness — Mission Impossible style. 🕴️",
    "🏦 {user} walks up to the teller with a smile and a note that reads: \"Act natural. Your vault code, please.\" 😏",
    "🏦 A massive power outage hits downtown. {user} flicks on a headlamp: \"Showtime.\" 🔦",

    # ── Casino ──
    "🎰 The crew blends into the casino crowd. {user} slips through the \"STAFF ONLY\" door while the others create a distraction at the roulette table… 🎲",
    "🎰 {user} sweet-talks the pit boss while the crew descends into the casino's counting room below… 💵",
    "🎰 Fake high-rollers, real criminals. {user} bets big to keep eyes on the floor while the vault team moves… 🃏",
    "🎰 \"The house always wins? Not tonight.\" {user} kills the lights from the server room. Chaos erupts. 🔌",
    "🎰 In matching tuxedos, the crew infiltrates the exclusive VIP lounge. {user} lifts the keycard from the manager's jacket pocket. 🤵",

    # ── Museum / Gallery ──
    "🏛️ __SMASH!__ 💎 Alarms ring as {user} _removes_ the top of the glass cabinet. It's full of jewels! The team better move quickly…",
    "🏛️ The guards change shift at midnight. {user} and the crew have exactly 7 minutes to swap the painting with a forgery… 🖼️",
    "🏛️ {user} triggers a fake fire alarm. While the museum evacuates, the crew walks out with a Rembrandt in a janitor's cart. 🧹",
    "🏛️ Infrared laser grid, pressure-sensitive floors. {user} stretches and limbers up: \"Hold my beer.\" 🤸",
    "🏛️ The crew poses as an art restoration team. {user} carefully \"restores\" the diamond right into a hidden compartment. 💎",
    "🏛️ {user} hacked the exhibit schedule to create a 20-minute window where the cameras loop old footage. The heist is ON. 🎥",

    # ── Tech / Cyber ──
    "💻 The crew targets a crypto exchange's cold storage. {user} plugs into the server rack: \"Transferring now…\" ₿",
    "💻 In a darkened server room, {user} types furiously. \"I'm in their firewall. Everyone stay quiet—\" *beep beep beep* 🔐",
    "💻 {user} deploys a custom zero-day exploit. The exchange's wallet drains into a thousand burner accounts. \"Ghost protocol.\" 👻",
    "💻 The crew intercepts a data center's hardware shipment. {user} swaps the drives mid-transit. \"They won't notice for weeks.\" 📦",
    "💻 {user} social-engineers the sysadmin with a fake pizza delivery. \"While he eats, I eat his database. 🍕💾\"",

    # ── Train ──
    "🚂 The overnight express thunders through the mountains. {user} leaps from horseback onto the mail car. \"OLD SCHOOL, BABY!\" 🤠",
    "🚂 The crew boards the luxury train posing as passengers. {user} slips sleeping powder into the security guards' coffee. ☕😴",
    "🚂 {user} decouples the gold-transport car from the rest of the train with a shower of sparks. \"This one's ours now!\" 🔥",
    "🚂 In the dining car, {user} spills wine on the conductor — \"So sorry!\" — while lifting the master key from his belt loop. 🍷🔑",

    # ── Yacht / Maritime ──
    "🚢 Under a moonless sky, the crew approaches the megayacht in a silent inflatable. {user} scales the hull with suction cups. 🌊",
    "🚢 {user} poses as the new private chef aboard the oligarch's yacht. The safe is behind the Monet in the master cabin. 👨‍🍳",
    "🚢 The crew hijacks a cargo container mid-shipment. {user} cracks the lock: \"Let's see what's worth $50 million…\" 📦⚓",
    "🚢 Scuba gear on, the crew descends to the sunken wreck. {user} pries open the captain's chest — centuries of gold within. 🤿",

    # ── Jewelry / Diamond ──
    "💎 The diamond exchange on 47th Street. {user} bypasses the vault's biometric scanner with a gummy-bear fingerprint. 🐻",
    "💎 {user} cuts a perfect circle in the skylight and drops into Tiffany's after hours. \"I'll take one of EVERYTHING.\" 💍",
    "💎 The crew tunnels from the pizza shop next door into the jeweler's vault. {user} breaks through: \"Extra toppings, baby!\" 🍕",
    "💎 {user} replaces the Hope Diamond with a cubic zirconia from Amazon. \"$19.99. They'll never know.\" 😏",

    # ── Underground / Mob ──
    "🎱 The mob boss's secret vault, hidden behind a pool table. {user} racks the 8-ball: \"This is for everything he owes us.\" 🎱",
    "🎱 {user} bribes the bouncer at the speakeasy. The real score is behind the false wall in the wine cellar. 🍷",
    "🎱 The cartel's money-counting room — cash stacked floor to ceiling. {user} opens the duffel bags: \"Fill 'em up.\" 💼💰",

    # ── Federal / Government ──
    "🏛️ Fort Knox. The most secure vault on the planet. {user} pulls out the blueprint: \"There's always a way in.\" 🗺️",
    "🏛️ \"Ladies and gentlemen, we're robbing the Federal Reserve.\" {user} adjusts their prosthetic face. \"Today, I am the Chairman.\" 🎭",
    "🏛️ The diplomatic motorcade stops at a red light. {user} swaps the briefcase in 4.7 seconds flat. \"That was the nuclear codes? Nah, just cash.\" 💼",

    # ── Misc / Wild ──
    "🏎️ The crew intercepts the Formula 1 prize convoy. {user} blocks the road with a stolen bus while the team grabs the trophy… and the sponsor cash. 🏆",
    "🚀 Space Station Heist! The crew breaches the orbital lab. {user} floats toward the prototype: \"This is worth billions — on ANY planet.\" 🛸",
    "🎪 Under the big top, the circus is a front. {user} crawls through the lion cage tunnel to reach the underground vault. \"Nice kitty…\" 🦁",
    "🎬 The movie studio's prop vault is full of \"fake\" gold. Except {user} knows half of it is REAL. \"Action!\" 🎥",
    "⛏️ The abandoned mine isn't abandoned at all. {user} finds the sealed chamber: \"They've been hiding this mother lode for years.\" ⛏️",
    "🏥 The evidence lockup in the courthouse basement. {user} dresses as a lawyer: \"I'd like to review exhibit A through Z... in a duffel bag.\" ⚖️",
]


# ══════════════════════════════════════════════════════════════
#  Win Lines — celebration when the heist succeeds
#  ~30 entries
# ══════════════════════════════════════════════════════════════

WIN_LINES: list[str] = [
    "💰 THAT WAS CLOSE! Sirens in the distance, but the crew vanishes into the night. Everyone collects {payout} {symbol}!",
    "💰 Like taking candy from a baby. Everyone collects {payout} {symbol}! 😎",
    "💰 The getaway driver floors it — tires screech, but the crew is CLEAN! Everyone collects {payout} {symbol}! 🚗💨",
    "💰 The doors slam shut behind them and the safe house erupts in cheers! Everyone collects {payout} {symbol}! 🎉",
    "💰 Not a single alarm tripped. The perfect crime. Everyone collects {payout} {symbol}! 🤌",
    "💰 {user} pulls off the mask and grins: \"Told you I'm the best in the business.\" Everyone collects {payout} {symbol}! 😏",
    "💰 The helicopter lifts off from the rooftop. The city shrinks below. \"We're rich, baby!\" Everyone collects {payout} {symbol}! 🚁",
    "💰 The fake wall seals shut behind them. No witnesses, no evidence, no problems. Everyone collects {payout} {symbol}! 🧱",
    "💰 Three shell companies, four offshore accounts, zero evidence. The money is CLEAN. Everyone collects {payout} {symbol}! 🏝️",
    "💰 The vault door closes behind them with a satisfying *click*. Wait — wrong vault. That's theirs. Everyone collects {payout} {symbol}! 🔐",
    "💰 \"Ladies and gentlemen, we are officially retired.\" Champagne pops as the crew splits {payout} {symbol} each. 🍾",
    "💰 The decoy ambulance races through red lights. Nobody stops an ambulance. Everyone collects {payout} {symbol}! 🚑",
    "💰 {user} tosses the keys to the vault into the river. \"That chapter is closed.\" Everyone collects {payout} {symbol}. 🔑🌊",
    "💰 The speedboat disappears into international waters before the first cop car arrives. Everyone collects {payout} {symbol}! 🚤",
    "💰 Not even a fingerprint left behind. The crew ghosts into the crowd. Everyone collects {payout} {symbol}! 👻",
    "💰 {user} shreds the burner phones. \"This crew was never here.\" Everyone collects {payout} {symbol}. 📱🔥",
    "💰 The loot fits perfectly in the false-bottom suitcases. Airport security waves them through. Everyone collects {payout} {symbol}! ✈️",
    "💰 Back at the warehouse, {user} dumps the bag on the table. Gold coins cascade everywhere. Everyone collects {payout} {symbol}! 🪙",
    "💰 The montage ends: crew driving into the sunset, shades on, music blasting. Everyone collects {payout} {symbol}! 🌅😎",
    "💰 \"I'd like to thank the academy… and this very trusting bank.\" Everyone collects {payout} {symbol}! 🏆",
    "💰 Clean in, clean out, clean getaway. Textbook. Everyone collects {payout} {symbol}! 📖✅",
    "💰 The news anchor reporting the heist has NO leads. The crew toasts from the couch. Everyone collects {payout} {symbol}! 📺🥂",
    "💰 {user} whispers into the earpiece: \"Package secured.\" The van peels out. Everyone collects {payout} {symbol}! 🎙️",
    "💰 The tunnel collapses behind them — no one's following THAT trail. Everyone collects {payout} {symbol}! 🕳️",
    "💰 The switch worked. The guards are still protecting an empty vault. Everyone collects {payout} {symbol}! 🃏",
    "💰 Ocean's got NOTHING on this crew. Flawless execution. Everyone collects {payout} {symbol}! 🎰",
    "💰 The hologram projector made it look like the jewels were still there. Genius. Everyone collects {payout} {symbol}! 🔮",
    "💰 \"And THAT is how it's done.\" *mic drop* — Everyone collects {payout} {symbol}! 🎤⬇️",
    "💰 The police sketch artist drew someone who looks NOTHING like {user}. Everyone collects {payout} {symbol}! 🎨👀",
    "💰 By the time the silent alarm calls for backup, the crew is three state lines away. Everyone collects {payout} {symbol}! 🗺️",
]


# ══════════════════════════════════════════════════════════════
#  Lose Lines — busted / failure
#  ~30 entries
# ══════════════════════════════════════════════════════════════

LOSE_LINES: list[str] = [
    "🚨 CAUGHT! {user} tripped the laser grid. Everyone loses their wager! 👮",
    "🚨 BUSTED! Undercover cops were waiting the whole time. The crew is going DOWNTOWN! 🚔",
    "🚨 {user} left prints on the vault door — the feds traced it in minutes. Wagers forfeited! 🔍",
    "🚨 The getaway van won't start! Surrounded by SWAT. It's over. Everyone's busted! 💀",
    "🚨 A dye pack exploded in the bag — {user} is covered in blue and the cops are closing in! 🔵👮",
    "🚨 {user} accidentally pocket-dialed 911 during the heist. You can't make this stuff up. 📱🤦",
    "🚨 The \"inside man\" was wearing a wire the ENTIRE TIME. The FBI has everything. Busted! 🎙️👮",
    "🚨 Helicopter spotlight locks onto the getaway car. \"PULL OVER!\" It's OVER. 🚁🔦",
    "🚨 {user} tried the vault code: 1-2-3-4. It was wrong. The real vault has already sealed. Wagers forfeited! 🔢",
    "🚨 The tunnel came up in the WRONG BUILDING. That's a Subway restaurant, not a bank. Everyone's arrested! 🥪👮",
    "🚨 {user} sneezed during the silent approach. Security heard it. The jig is up. 🤧",
    "🚨 K-9 unit found the stash. Good boy. Bad day for the crew. Wagers forfeited! 🐕‍🦺",
    "🚨 The disguise fell off mid-heist. {user}'s face is on every camera in the building. 📸",
    "🚨 Plot twist: the getaway driver was an undercover cop ALL ALONG. Handcuffs for everyone! 🚗👮",
    "🚨 {user} grabbed the wrong bag. It's full of expired coupons. The cops grab the right one… and the crew. 🏷️",
    "🚨 The security guard was a retired Navy SEAL. He subdued the entire crew. Solo. 💪👮",
    "🚨 \"We're professionals,\" said {user}, moments before the smoke alarm went off from their own smoke bomb. 💨🚨",
    "🚨 The vault was empty — the real money was moved yesterday. And now the cops are here. Double L. 📭👮",
    "🚨 {user} forgot to disable the GPS tracker in the cash. The police followed the money right to the hideout. 📡",
    "🚨 The crew's matching ski masks were on the store's loyalty cam from when they BOUGHT them. Trail of evidence! 🎿📸",
    "🚨 Spike strips on every exit. The tires are shredded. The crew walks out with hands up. 🛞👐",
    "🚨 The thermal cameras saw right through the disguises. {user}'s body heat gave them all away. 🌡️",
    "🚨 {user} tripped on a cat in the vault. Yes, a cat. The cat pressed the silent alarm. 🐈🚨",
    "🚨 The judge was NOT amused. \"You robbed a bank… dressed as clowns?\" Wagers forfeited! 🤡⚖️",
    "🚨 Air duct was too small. {user} got stuck. The fire department AND the police showed up. 🧯🚒",
    "🚨 The crew's walkie-talkies were on a police frequency. The cops heard the whole plan. 📻👮",
    "🚨 \"Don't worry, the cameras are off,\" said {user}. They were NOT off. Captured in 4K. 📹",
    "🚨 The getaway boat had a hole in it. The crew sank 200 meters off shore. Coast Guard picks them up. 🚤🕳️",
    "🚨 {user}'s fake ID said \"McLovin\". The guard did NOT let them through. 🪪",
    "🚨 Plot twist: the entire building was a police sting operation. There was never any money. Wagers forfeited! 🎭👮",
]


# ══════════════════════════════════════════════════════════════
#  Push Lines — near-miss, partial refund
#  ~15 entries
# ══════════════════════════════════════════════════════════════

PUSH_LINES: list[str] = [
    "😰 The alarm trips! The crew scatters — most of the loot falls out of the bags during the escape. Refunded minus a 5% \"dry cleaning\" fee.",
    "😰 A guard spots the crew at the last second — they bail but drop most of the cash. Refunded minus 5% for the getaway fuel. ⛽",
    "😰 {user} accidentally sets off a smoke bomb in the van. Chaos. The crew saves MOST of the take… minus 5%.",
    "😰 The vault timer was shorter than expected. The crew grabs what they can and bolts — 5% lost in the rush.",
    "😰 {user} slips on a wet floor sign. Half the cash scatters. The crew saves most of it, minus 5% \"hazard pay\". 🫠",
    "😰 The crew makes it out but the dye pack pops on the last bag. 95% saved — 5% is permanently blue. 🔵",
    "😰 A security drone spots the getaway. The crew ditches 5% of the haul as a decoy. It works… barely. 🤖",
    "😰 The helicopter ran out of fuel mid-escape. Emergency landing. The crew survives with 95% of the score. 😅🚁",
    "😰 A rival crew shows up! Tense standoff. {user} negotiates: \"You take 5%. We walk.\" Deal. 🤝",
    "😰 The back door was WELDED SHUT. By the time {user} finds another exit, 5% of the take is left behind. ⏱️",
    "😰 Cops set up a roadblock but the crew knows a shortcut. Lost 5% of the haul bouncing through an alley. 🚙💨",
    "😰 One of the bags ripped on the fence. Bills fly everywhere. The crew saves 95% and runs. 💸",
    "😰 \"DROP THE BAGS!\" screams the guard. {user} drops ONE decoy bag (5%) and the crew escapes with the rest. 🎒",
    "😰 The heat was REAL. The crew had to bribe a boat captain with 5% of the haul to escape across the river. 🛶",
    "😰 The vault's secondary lock engaged mid-grab. The crew pries it open but loses 5% of the goods in the scramble. 🔒",
]


# ══════════════════════════════════════════════════════════════
#  Join Lines — crew member joining announcements
#  ~40 entries
# ══════════════════════════════════════════════════════════════

JOIN_LINES: list[str] = [
    # ── Classic movie tropes ──
    "🔫 \"You son of a bitch, I'm in!\" — {user}",
    "🤝 \"One last job. After this, we're even. Understood?\" — {user}",
    "😏 {user} cracks their knuckles. \"Let's do this.\"",
    "🎭 {user} puts on the mask. \"Nobody knows me in there.\"",
    "🗺️ \"I know a guy on the inside…\" — {user}",
    "💣 {user} opens a briefcase full of explosives. \"I brought party favors.\"",
    "🕶️ {user} slides on sunglasses. \"I was born for this.\"",
    "🤫 {user} slips in through the back. \"What? I was already here.\"",
    "🔒 \"I can crack any safe in under 60 seconds.\" — {user}",
    "🏎️ {user} revs the engine. \"I'll be the getaway.\"",

    # ── Dramatic entrances ──
    "🎵 {user} walks in slow-motion to dramatic music. They're SO in.",
    "☕ {user} sips coffee and sets it down. \"Alright. What are we stealing?\"",
    "🧤 {user} pulls on leather gloves. Finger by finger. No words needed.",
    "📞 {user} hangs up the phone. \"That was my retirement plan calling. I told it to wait.\"",
    "🃏 {user} flips a coin. Catches it. \"I'm in. But I get first pick of the loot.\"",
    "🕴️ {user} steps out of the shadows. \"You didn't think you'd pull this off without ME, did you?\"",
    "🛠️ {user} lays out a custom toolkit. \"I've been preparing for this my whole life.\"",
    "🍿 {user} puts down the popcorn. \"Wait — this is real? Not a movie? …I'm DEFINITELY in.\"",
    "📱 {user} smashes their phone. \"No traces. Let's ride.\"",
    "🧠 \"I've memorized the floor plan, guard rotations, and the lunch menu.\" — {user}",

    # ── Comedy / personality ──
    "🎒 {user} shows up with a backpack full of snacks. \"What? Heists make me hungry!\" 🍫",
    "🐎 {user} arrives on a horse. \"What? It's an ESCAPE plan.\"",
    "💅 {user} finishes filing their nails. \"Fine. I'll come. But I'm NOT carrying anything heavy.\"",
    "🧁 {user} brings cupcakes for the crew. \"For morale. Also, I'm in.\" 🧁",
    "🤖 {user} sends a very suspicious robot in their place. Just kidding — they're right behind it.",
    "📝 {user} signs a waiver. \"Just in case. I know a good lawyer.\"",
    "🎩 {user} tips their hat. \"I heard there was money. And danger. My two favorite things.\"",
    "🦝 {user} shows up in full raccoon costume. \"For the STEALTH. Obviously.\"",
    "🔧 \"I modified the van. It now has NOS, smoke screen, and cup holders.\" — {user}",
    "💼 {user} opens a briefcase. It's empty. \"This is for AFTER.\" 😏",

    # ── Specialist roles ──
    "🔓 \"Safecracker, reporting for duty.\" — {user}",
    "🖥️ \"I'll handle the security system. Just… don't unplug anything.\" — {user}",
    "🚗 \"I can outrun any cop car on the road. Trust me.\" — {user}",
    "🎯 \"I'm the lookout. Nothing gets past these eyes.\" — {user}",
    "🏋️ \"Need someone to carry heavy things? That's my job.\" — {user}",
    "🎭 \"I'll be the distraction. I'm VERY distracting.\" — {user}",
    "🧪 \"I brought knock-out gas. Homemade. Mostly safe.\" — {user}",
    "🪝 \"Grappling hooks, night vision, the works. Let's go.\" — {user}",
    "🎪 \"I used to work in that building. I know every exit.\" — {user}",
    "🗝️ \"I already copied the master key last Tuesday. You're welcome.\" — {user}",
]
