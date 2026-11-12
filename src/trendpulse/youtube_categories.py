"""YouTube videoCategoryId reference data (global ids, stable across regions)."""

CATEGORY_NAMES: dict[int, str] = {
    1: "Film & Animation", 2: "Autos & Vehicles", 10: "Music", 15: "Pets & Animals", 17: "Sports",
    18: "Short Movies", 19: "Travel & Events", 20: "Gaming", 21: "Videoblogging", 22: "People & Blogs",
    23: "Comedy", 24: "Entertainment", 25: "News & Politics", 26: "Howto & Style", 27: "Education",
    28: "Science & Technology", 29: "Nonprofits & Activism", 30: "Movies", 31: "Anime/Animation",
    32: "Action/Adventure", 33: "Classics", 34: "Comedy", 35: "Documentary", 36: "Drama", 37: "Family",
    38: "Foreign", 39: "Horror", 40: "Sci-Fi/Fantasy", 41: "Thriller", 42: "Shorts", 43: "Shows",
    44: "Trailers",
}

# Fallback mapping from a YouTube category to a TrendPulse taxonomy category,
# used only when keyword evidence is absent.
TAXONOMY_CATEGORY_FALLBACK: dict[int, str] = {
    1: "Entertainment", 2: "Lifestyle", 10: "Music", 15: "Lifestyle", 17: "Sports", 19: "Lifestyle",
    20: "Gaming", 22: "Lifestyle", 23: "Entertainment", 24: "Entertainment", 25: "News & Politics",
    26: "Lifestyle", 27: "Education", 28: "Technology", 29: "News & Politics", 30: "Entertainment",
    43: "Entertainment", 44: "Entertainment",
}


def category_name(category_id: int | None) -> str | None:
    if category_id is None:
        return None
    return CATEGORY_NAMES.get(int(category_id))
