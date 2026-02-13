  Quick start:
  1. cp .env.example .env → add your PRIVATE_KEY
  2. .venv/bin/python3 client.py --derive-keys → paste creds into .env
  3. .venv/bin/python3 discovery.py → dry-run to see what markets are found
  4. .venv/bin/python3 main.py → run the bot
  5. .venv/bin/python3 inventory.py to dump inventory

Inventory risk management:
 1. Quote skewing — Shift your bid/ask based on inventory. If you're long (holding YES tokens), lower both your bid and ask. This makes your sell more attractive and  
  your buy less likely to fill, naturally pushing inventory back to zero.                                                                                               
  2. Size asymmetry — Reduce order size on the side that would add to your position. If you're long YES, post smaller bids (or stop bidding entirely) while keeping full
   size on the sell.                                                         
  3. Wider spreads under exposure — Widen your spread when holding inventory to compensate for the risk. Narrower when flat, wider when exposed.
  4. Hard inventory caps — Define a max position. Once hit, stop quoting the side that adds exposure entirely until you're back within limits.
  5. Time decay on exit price — The longer you hold, the more aggressively you price the exit. Start at the ask, and if it hasn't filled after N seconds, gradually walk
   the price down toward mid, then eventually market-sell as a last resort.
  6. Stale inventory dump — If a position has been held beyond some threshold (say 30s or 60s), just market-sell and cut the loss. Treats the market order as a
  stop-loss rather than the default exit.


No midprice bug btw

add stop loss/take profit system
test if quoting while exiting positions work

how does polymarket fill orders? are they a queue? does refreshing the orders make it less likely to get them filled?

got adversarially selected against for sure on the mr beast market. why? what happened?
