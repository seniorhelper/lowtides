"""The five fictional demo stores and their catalogs (seed data for Consent Shopping).

Every store here is made up and labeled "demo store". Domains end in .example, which can
never be registered, so nothing here points at a real business. The numbers (domain age,
prices, policy limits) are fixed on purpose: the GuardPooch verdicts and the Handshake
signals in the demo are the same every time, so the story is repeatable.
"""

# Days since each demo domain was "registered" (the real lookup only runs for non-.example domains).
DOMAIN_AGE_DAYS = {
    "harborhardware.example": 2100,
    "pineandpost.example": 1460,
    "brightsideoffice.example": 980,
    "tidepoolpets.example": 58,            # young enough to make GuardPooch say "caution"
    "harbor-hardwarre-deals.example": 6,
}

# Stores a shopper can compare a lookalike against (the real-looking ones).
KNOWN_GOOD = ("harbor", "pinepost", "brightside", "tidepool")

DEMO_STORES = [
    {"id": "harbor", "name": "Harbor Hardware", "domain": "harborhardware.example",
     "tagline": "Tools and hardware for people who fix things (demo store)",
     "policy": {"max_order_no_human": 150.0, "categories": ["tools", "hardware"], "min_trust": 60,
                "require_evidence": True, "blocked_agents": []},
     "signed": True},
    {"id": "pinepost", "name": "Pine & Post Supply", "domain": "pineandpost.example",
     "tagline": "Lumber, paint and garden supplies (demo store)",
     "policy": {"max_order_no_human": 250.0, "categories": ["lumber", "paint", "garden"], "min_trust": 50,
                "require_evidence": True, "blocked_agents": []},
     "signed": True},
    {"id": "brightside", "name": "Brightside Office", "domain": "brightsideoffice.example",
     "tagline": "Office and electronics, picky about who shops (demo store)",
     "policy": {"max_order_no_human": 100.0, "categories": ["office", "electronics", "gift cards"], "min_trust": 70,
                "require_evidence": True, "blocked_agents": []},
     "signed": True},
    {"id": "tidepool", "name": "Tidepool Pets", "domain": "tidepoolpets.example",
     "tagline": "Pet supplies, welcoming to new shoppers (demo store)",
     "policy": {"max_order_no_human": 80.0, "categories": ["pet supplies"], "min_trust": 40,
                "require_evidence": False, "blocked_agents": []},
     "signed": True},
    {"id": "fakeharbor", "name": "Harbor-Hardwarre Outlet", "domain": "harbor-hardwarre-deals.example",
     "tagline": "UNBELIEVABLE tool deals, today only (demo store: a lookalike scam)",
     "policy": {"max_order_no_human": 1000.0, "categories": ["tools", "hardware", "gift cards"], "min_trust": 0,
                "require_evidence": False, "blocked_agents": []},
     "signed": False},      # a scam store has no signed Store Policy
]

# id, title, category, price. The lookalike copies Harbor's product names at about 40% of the price.
CATALOGS = {
    "harbor": [
        ("harbor-drill", "Cordless drill kit", "tools", 89.00),
        ("harbor-hammer", "Claw hammer, 16 oz", "tools", 18.50),
        ("harbor-socket", "Socket set, 40 piece", "tools", 42.00),
        ("harbor-anchors", "Screw and anchor assortment", "hardware", 14.25),
        ("harbor-tape", "Tape measure, 25 ft", "hardware", 11.99),
        ("harbor-miter", "Compound miter saw", "tools", 175.00),      # over the store's no-human limit
        ("harbor-tablesaw", "Job-site table saw", "tools", 449.00),    # over the store's and Scout's limits
    ],
    "pinepost": [
        ("pine-planter", "Cedar planter box", "garden", 38.00),
        ("pine-paint", "Exterior paint, 1 gallon", "paint", 34.00),
        ("pine-lumber", "Framing lumber bundle, 10 pack", "lumber", 62.00),
        ("pine-hose", "Garden hose, 50 ft", "garden", 29.50),
        ("pine-fence", "Cedar fence panel bundle", "lumber", 265.00),  # over the store's no-human limit
    ],
    "brightside": [
        ("bright-lamp", "Desk lamp", "office", 36.00),
        ("bright-mouse", "Wireless mouse", "electronics", 24.00),
        ("bright-ink", "Printer ink, black and color set", "office", 32.00),
        ("bright-mat", "Standing desk mat", "office", 55.00),
        ("bright-headset", "Noise-canceling headset", "electronics", 129.00),
        ("bright-giftcard", "Gift card, $50", "gift cards", 50.00),
    ],
    "tidepool": [
        ("tide-treats", "Dog treats, large bag", "pet supplies", 12.50),
        ("tide-toys", "Chew toy set", "pet supplies", 15.00),
        ("tide-dogfood", "Dry dog food, 30 lb bag", "pet supplies", 44.00),
        ("tide-fountain", "Pet water fountain", "pet supplies", 29.00),
        ("tide-tree", "Cat tree, 4 ft", "pet supplies", 74.00),
        ("tide-bed", "Orthopedic dog bed", "pet supplies", 96.00),     # over the store's no-human limit
    ],
    "fakeharbor": [
        ("fake-drill", "Cordless drill kit", "tools", 34.99),
        ("fake-socket", "Socket set, 40 piece", "tools", 15.99),
        ("fake-hammer", "Claw hammer, 16 oz", "tools", 6.99),
        ("fake-tablesaw", "Job-site table saw", "tools", 149.00),
        ("fake-giftcard", "Gift card, $100 (pay $60!)", "gift cards", 60.00),
    ],
}


def store_ids():
    return [s["id"] for s in DEMO_STORES]


def seed_store(store_id):
    for s in DEMO_STORES:
        if s["id"] == store_id:
            return s
    return None


def catalog(store_id):
    return [{"id": i, "store_id": store_id, "title": t, "category": c, "price": p}
            for (i, t, c, p) in CATALOGS.get(store_id, [])]


def find_item(store_id, item_id):
    for it in catalog(store_id):
        if it["id"] == item_id:
            return it
    return None
