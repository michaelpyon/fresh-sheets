# Fresh Sheets: Audience Pass Suggestions

Last refreshed 2026-05-30. Builds on the prior pass that replaced the dead
forms.gle CTA with an inline localStorage email capture (commit da42aa5).

## Evangelist (the one person who loves AND shares this)

A 27 year old in r/CleaningTips and r/adhdwomen who knows her sheets are gross,
feels guilty about it, and has tried and abandoned every habit app. She does not
want another dashboard to check, she wants a text that tells her what to do. She
currently relies on a vague mental note or a recurring calendar alert she
dismisses without acting. She screenshots Fresh Sheets because the headline
"When did you last wash your sheets? Be honest." is funny and calls her out, and
because "reply DONE, that's it" is the lowest friction habit loop she has seen.
She bounces in 5 seconds if the page looks like a generic SaaS signup, if it asks
for her phone number before explaining what happens, or if she suspects the
waitlist is a black hole that goes nowhere. The honest "saved in this browser"
copy is good for trust but currently gives her no real way to actually reach the
maker, so the most motivated visitor hits a dead end.

## Ground-truth findings (repo HEAD)

WORKING and HONEST in HEAD:
- frontend/index.html is the deployed artifact (vercel.json builds dist/ from it).
- Primary CTA is an inline email capture, validated client side, saved to
  localStorage under freshSheets_waitlistEmail. Returning visitors see the
  confirmation immediately. No dead forms.gle link in HEAD.
- Confirmation copy is honest: it states the email was saved in this browser only
  and no server has received it yet. No fabricated counts, no fake "real-time" or
  "updated daily" claims, no invented testimonials, no random number generators.
- dist/index.html matches frontend/index.html exactly.
- Backend (app.py, sms.py, scheduler.py, models.py) is a real Flask + Twilio +
  APScheduler app with a passing pytest suite, not deployed for this landing page.

DEPLOY NEEDED (not a re-fix):
- The LIVE site at https://fresh-sheets.vercel.app still serves the OLD build:
  its primary CTA is the dead href="https://forms.gle/PLACEHOLDER". The repo fix
  has been committed and pushed but never deployed. The next Vercel deploy from
  HEAD ships the working inline form. FLAG: deploy required.

GAP (honest, prior pass flagged it):
- The localStorage capture collects nothing a human ever sees. The most motivated
  visitor has no real path to reach Michael. This is the highest-leverage honest
  fix short of a real backend endpoint.

## Shipped wave 2 (2026-05-30)

- Item 2 SHIPPED: added a "How it works" 3 step strip (Pick a cadence, Get a
  text, Reply DONE) between the headline and the waitlist form. Gives the
  evangelist the actual product loop in seconds before she decides, reducing the
  5 second bounce. Additive, responsive (3 columns on desktop, stacked under
  560px), no new deps. File: frontend/index.html (built into dist). Builds and
  parses clean. Deploy still needed to verify live.

Note: item 1 (zero-backend mailto bridge) was shipped in the prior wave
(commit 6f878b2). og.png was verified as a real 1200x630 PNG in HEAD, so the
share card already rasterizes correctly; item 3 below is now only a file-size
nicety, not a correctness fix.

## Prioritized plan

### Quick wins

1. DONE (prior wave, commit 6f878b2): zero-backend mailto delivery path after a
   valid email is captured to localStorage. Reveals an "Email Michael to hold my
   spot" mailto prefilled with the address and subject.

2. DONE (wave 2): "How it works" 3 step strip above the form. See Shipped wave 2.

3. Tighten the OG image freshness check. og.png is 306 KB which is heavy for a
   share card. Re-export at a smaller size to speed share previews on Reddit and
   iMessage. File: frontend/og.png via the build. Effort S. Deploy to verify.

### Bigger bets

4. Wire a real signup endpoint (ACTION for Michael). Formspree free tier or a tiny
   Vercel serverless function writing to a sheet or KV. Replace the localStorage
   stopgap so emails actually reach a destination. Effort M. Deploy needed. This
   is the true unlock; until then the mailto in item 1 is the honest bridge.

5. Connect the deployed landing page to the real Flask backend that already
   exists. The frontend supports window.FRESH_SHEETS_API_BASE pointing at a
   Railway backend; deploying app.py and setting that base would make the full
   phone verify and SMS flow live. Effort L. Deploy needed.

6. Add light social proof that is true, not invented. A single honest line like
   the count of people on the local waitlist is risky (localStorage is per
   browser). Skip fabricated counts. Instead add a one line founder note ("Built
   by Michael, a person who also forgets to wash his sheets") for authenticity.
   Effort S. Deploy to verify.

7. Add a meta description and OG copy A/B candidate that leads with the funny
   hook ("Be honest: when did you last wash your sheets?") since that is the
   screenshot line. File: frontend/index.html head. Effort S. Deploy to verify.
