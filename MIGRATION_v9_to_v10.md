# Migrating from SDK 9 to 10

SDK 10 targets AdCP 3.3. Review the changes below before upgrading.

| Change | Update your integration |
| --- | --- |
| Pixel trackers for AdCP 3.3 playback events are dropped when downgraded to the v1 url-tracker shape | `downgrade_pixel_tracker` returns `PixelTrackerDowngrade(v1=None, ...)` with a `PIXEL_TRACKER_LOSSY_DOWNGRADE` advisory for `start`, `first_quartile`, `midpoint`, `third_quartile` and `complete`, and for their 3.2 interim form (`event: custom` with `custom_event_name` set to one of them). SDK 9 put the interim form in the `impression_tracker` slot, which counted every impression as a quartile view. Check `result.v1 is not None` before reading it. `downgrade_pixel_trackers` leaves dropped trackers out of `items`. See the AdCP [3.2 to 3.3 migration guide](https://docs.adcontextprotocol.org/docs/reference/migration/3-2-to-3-3#hosted-video-and-audio-playback-trackers). |
