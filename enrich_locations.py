"""
Parse the messy free-text `jobs.location` strings into a normalized
job_locations table (job_id, city, country) so the website can filter
jobs country-wise and city-wise. One job can map to several locations.

Idempotent: re-parses every job each run (table is rebuilt from jobs).
"""

import re

import db

# city (lowercase) -> (canonical city, country)
CITIES = {
    # United States
    "san francisco": ("San Francisco", "United States"),
    "sf": ("San Francisco", "United States"),
    "new york": ("New York", "United States"),
    "nyc": ("New York", "United States"),
    "seattle": ("Seattle", "United States"),
    "sea": ("Seattle", "United States"),
    "chicago": ("Chicago", "United States"),
    "chi": ("Chicago", "United States"),
    "boston": ("Boston", "United States"),
    "washington": ("Washington D.C.", "United States"),
    "los angeles": ("Los Angeles", "United States"),
    "san diego": ("San Diego", "United States"),
    "denver": ("Denver", "United States"),
    "austin": ("Austin", "United States"),
    "atlanta": ("Atlanta", "United States"),
    "miami": ("Miami", "United States"),
    "santa clara": ("Santa Clara", "United States"),
    "sunnyvale": ("Sunnyvale", "United States"),
    "mountain view": ("Mountain View", "United States"),
    "cupertino": ("Cupertino", "United States"),
    "redmond": ("Redmond", "United States"),
    "bellevue": ("Bellevue", "United States"),
    "portland": ("Portland", "United States"),
    # Canada
    "toronto": ("Toronto", "Canada"),
    "montreal": ("Montreal", "Canada"),
    "vancouver": ("Vancouver", "Canada"),
    "ontario": (None, "Canada"),
    # Latin America
    "mexico city": ("Mexico City", "Mexico"),
    "são paulo": ("São Paulo", "Brazil"),
    "sao paulo": ("São Paulo", "Brazil"),
    # Europe
    "london": ("London", "United Kingdom"),
    "dublin": ("Dublin", "Ireland"),
    "paris": ("Paris", "France"),
    "berlin": ("Berlin", "Germany"),
    "munich": ("Munich", "Germany"),
    "madrid": ("Madrid", "Spain"),
    "barcelona": ("Barcelona", "Spain"),
    "milan": ("Milan", "Italy"),
    "amsterdam": ("Amsterdam", "Netherlands"),
    "zurich": ("Zurich", "Switzerland"),
    "warsaw": ("Warsaw", "Poland"),
    "stockholm": ("Stockholm", "Sweden"),
    # Asia & Middle East
    "singapore": ("Singapore", "Singapore"),
    "tokyo": ("Tokyo", "Japan"),
    "seoul": ("Seoul", "South Korea"),
    "bengaluru": ("Bengaluru", "India"),
    "bangalore": ("Bengaluru", "India"),
    "gurugram": ("Gurugram", "India"),
    "gurgaon": ("Gurugram", "India"),
    "mumbai": ("Mumbai", "India"),
    "hyderabad": ("Hyderabad", "India"),
    "new delhi": ("New Delhi", "India"),
    "shanghai": ("Shanghai", "China"),
    "beijing": ("Beijing", "China"),
    "shenzhen": ("Shenzhen", "China"),
    "hong kong": ("Hong Kong", "Hong Kong"),
    "taipei": ("Taipei", "Taiwan"),
    "tel aviv": ("Tel Aviv", "Israel"),
    "dubai": ("Dubai", "United Arab Emirates"),
    # Oceania
    "sydney": ("Sydney", "Australia"),
    "melbourne": ("Melbourne", "Australia"),
}

COUNTRIES = {
    "united states": "United States", "usa": "United States",
    "us": "United States", "u.s.": "United States",
    "united kingdom": "United Kingdom", "uk": "United Kingdom",
    "ireland": "Ireland", "ie": "Ireland",
    "india": "India", "china": "China", "japan": "Japan",
    "brazil": "Brazil", "canada": "Canada", "can": "Canada",
    "mexico": "Mexico", "germany": "Germany", "france": "France",
    "spain": "Spain", "italy": "Italy", "singapore": "Singapore",
    "south korea": "South Korea", "australia": "Australia",
    "netherlands": "Netherlands", "switzerland": "Switzerland",
    "israel": "Israel", "poland": "Poland", "sweden": "Sweden",
}

US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc", "california",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS job_locations (
    job_id  BIGINT REFERENCES jobs(id) ON DELETE CASCADE,
    city    TEXT,
    country TEXT,
    UNIQUE (job_id, city, country)
);
CREATE INDEX IF NOT EXISTS job_loc_country_idx ON job_locations (country);
CREATE INDEX IF NOT EXISTS job_loc_city_idx    ON job_locations (city);
"""

WORD = re.compile(r"[a-zà-ÿ.]+(?:[ -][a-zà-ÿ.]+)*")


def parse_location(raw):
    """Free-text location -> set of (city, country) pairs."""
    if not raw:
        return set()
    text = raw.lower()
    found = set()

    # longest city names first so "new york city" beats "york"
    for key in sorted(CITIES, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", text):
            found.add(CITIES[key])

    countries_hit = {c for _, c in found if c}
    for token in re.split(r"[;|,/&]| and | - ", text):
        token = token.strip().strip(".")
        if token in COUNTRIES:
            countries_hit_before = COUNTRIES[token] in countries_hit
            if not countries_hit_before:
                found.add((None, COUNTRIES[token]))
                countries_hit.add(COUNTRIES[token])
        elif token in US_STATES and "United States" not in countries_hit:
            found.add((None, "United States"))
            countries_hit.add("United States")

    if "remote" in text:
        country = next(iter(countries_hit)) if len(countries_hit) == 1 else None
        found.add(("Remote", country))

    return found


def main():
    conn = db.connect()
    conn.execute(SCHEMA)
    conn.execute("DELETE FROM job_locations")

    jobs = conn.execute("SELECT id, location FROM jobs").fetchall()
    rows, unparsed = [], 0
    for j in jobs:
        pairs = parse_location(j["location"])
        if not pairs:
            unparsed += 1
        rows.extend((j["id"], city, country) for city, country in pairs)

    conn.cursor().executemany(
        """INSERT INTO job_locations (job_id, city, country)
           VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""", rows)
    conn.commit()

    n = conn.execute(
        "SELECT COUNT(*) AS n FROM job_locations").fetchone()["n"]
    countries = conn.execute(
        "SELECT COUNT(DISTINCT country) AS n FROM job_locations"
        " WHERE country IS NOT NULL").fetchone()["n"]
    cities = conn.execute(
        "SELECT COUNT(DISTINCT city) AS n FROM job_locations"
        " WHERE city IS NOT NULL").fetchone()["n"]
    print(f"{len(jobs)} jobs -> {n} location rows "
          f"({countries} countries, {cities} cities, "
          f"{unparsed} jobs unparseable e.g. 'N Locations')")
    conn.close()


if __name__ == "__main__":
    main()
