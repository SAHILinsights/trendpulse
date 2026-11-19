# TrendPulse privacy policy

_Last updated: 20 November 2026_

TrendPulse (<https://trendpulse-sahil.streamlit.app>) is a non-commercial, open-source portfolio
project by Sahil Sharma that analyses public YouTube trending data. Source code:
<https://github.com/SAHILinsights/trendpulse>.

## What TrendPulse collects

- **No personal data about visitors.** The dashboard has no sign-up, no login for viewers, no
  cookies or trackers set by TrendPulse, and no forms that collect personal information.
- **Public YouTube data only.** Through the YouTube Data API (API key, no OAuth) TrendPulse reads
  public video metadata and statistics: video and channel ids, titles, channel names, tags,
  category, publish time, and view, like and comment counts. It never accesses any user's private
  data or YouTube account.

## How the data is used and stored

The data is used only to compute and display trend analytics: which topics are trending in which
countries and how fast they grow. It is stored in a private PostgreSQL database that is not
exposed through any public API. Retention:

| Data | Kept for |
|---|---|
| Raw API responses | 2 days |
| Video and channel metadata | refreshed at least every 30 days, otherwise deleted |
| Video-level statistics | 30 days |
| Topic-level aggregates | 400 days |

Data is not sold, shared or combined with data from other platforms.

## Third-party services

- **YouTube API Services.** TrendPulse uses YouTube API Services. By using TrendPulse you agree to
  the [YouTube Terms of Service](https://www.youtube.com/t/terms). See the
  [Google Privacy Policy](https://policies.google.com/privacy).
- **Hosting.** The dashboard is hosted on Streamlit Community Cloud, which may keep standard
  server logs under its own privacy policy.

## Contact and removal requests

Open an issue at <https://github.com/SAHILinsights/trendpulse/issues>. Requests to remove data
about a specific video or channel are honoured promptly. Data from videos removed from YouTube is
deleted by the refresh rules above.
