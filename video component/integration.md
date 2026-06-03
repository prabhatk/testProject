#How to integrate
###1. Configure the component — at the top of the <script> block:
jsconst CONFIG = {
  apiUrl:       'https://your-api.com/videos',   // ← your endpoint
  theoLicense:  'YOUR_THEO_LICENSE',              // ← from THEO portal
  autoPlayFirst: true,                            // optional
  pollInterval:  30000,                           // optional live refresh (ms)
  fieldMap: { ... }                               // map your API field names
};

###2. Update the THEO CDN URLs — replace YOUR_THEO_LICENSE in the two <link>/<script> tags in the <head>.

###3. Embed anywhere with an iFrame:
<iframe
  src="https://your-host.com/video-player-component.html"
  width="900"
  height="540"
  frameborder="0"
  allowfullscreen
  allow="autoplay; encrypted-media; fullscreen">
</iframe>
