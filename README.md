# OptionDesk — Quantitative Options Engine & Live Signal Terminal

**OptionDesk** is a production-grade algorithmic options trading and signal generation system designed around the core principles of **tastylive's "Mike and His Whiteboard"** curriculum (121 lessons), tailored specifically to the **National Stock Exchange of India (NSE)** market structure and **Angel One SmartAPI**.

---

## 1. What You Learn From the Course vs. How OptionDesk Automates It

| Concept in tastylive Course | The Theoretical Rule | How OptionDesk Implements It in Code |
| :--- | :--- | :--- |
| **Buying vs Selling Options** (V11) | Selling has higher POP; buying suffers theta decay unless timed with low IV and trend. | **Dual Engine**: Premium selling for high IV rank (Nifty spreads), premium buying for low IV rank (next-month stocks). |
| **IV Rank & Percentile** (V42, V48) | Absolute IV means nothing; relative IV tells you whether options are expensive or cheap. | `iv_context()` tracks 365-day trailing IV; rules enforce IV Rank $\ge 25$ for credit selling and $\le 32$ for option buying. |
| **Delta as Moneyness & Probability** (V15, V52) | Delta estimates the probability of expiring ITM ($\| \Delta \| \approx P(\text{ITM})$). | Strike selection automatically scans for $\sim 0.16\ \Delta$ short strikes ($\sim 84\%$ POP) and $0.55\text{--}0.75\ \Delta$ ITM for long options. |
| **Iron Condors & Credit Spreads** (V4, V27, V50) | Directional defined risk vs non-directional delta-neutral range trading. | Built-in constructors calculate wings, exact piecewise linear payoff curves, breakevens, and POP. |
| **Credit vs Width Rule** (V37) | Collect $\approx 1/3$ the spread width at 45 DTE to ensure positive expected value ($EV > 0$). | Scaled credit threshold: $\text{Min Credit} \ge 30\% \times \sqrt{\frac{\text{DTE}}{45}}$, ensuring weekly contracts (3–7 DTE) and monthly contracts are sized fairly. |
| **Managing at 50% Profit** (V53) | Holding to 100% expiration takes disproportionate time for tiny remaining edge. | Every signal emits an automated **Exit Rule**: take profit at 50% max profit, stop loss at $2.0\times$ credit received. |
| **Gamma Explosion & Expiration Risk** (V10, V22) | Gamma peaks in the final 48 hours; pin risk and tail jumps wipe out weeks of decay. | Automated exit/roll trigger at **2 DTE** for index credit spreads and **7–10 DTE** for stock option buyers. |

---

## 2. Core Trading Strategies

### Strategy A: Nifty Hedged Spreads (Premium Selling)
* **Goal**: Collect rich extrinsic value while capping maximum risk with a protective wing.
* **Underlying**: NIFTY 50 index (Tuesday weekly expiry or last Tuesday monthly expiry).
* **Conditions**:
  1. **IV Rank**: Must be $\ge 25\%$ (options are elevated relative to historical vol).
  2. **Short Strike Selection**: Nearest liquid strike to $\sim 0.16\ \Delta$ ($\sim 84\%$ probability of finishing out-of-the-money).
  3. **Wing Strike**: Delta $\sim 0.05$ or 200–250 points out (whichever provides wider buffer).
  4. **Directional Bias**:
     - Trend **Bullish** (Price $>$ SMA20 $>$ SMA50): **Bull Put Credit Spread** (Sell OTM Put, Buy further OTM Put).
     - Trend **Bearish** (Price $<$ SMA20 $<$ SMA50): **Bear Call Credit Spread** (Sell OTM Call, Buy further OTM Call).
     - Trend **Neutral**: **Iron Condor** (Sell OTM Put spread + OTM Call spread).
  5. **POP Gate**: Joint probability of short strikes expiring worthless must be $\ge 68\%$ for single spreads ($\ge 60\%$ for condors).

### Strategy B: Next-Month Stock Option Buying (Long Premium)
* **Goal**: Capture directional momentum and expansion of volatility on single stocks without getting slaughtered by rapid theta decay.
* **Underlying**: NSE F&O stock universe (Reliance, TCS, Infy, HDFC Bank, ICICI Bank, Tata Motors, etc.).
* **Conditions**:
  1. **IV Rank Gate**: Must be $\le 32\%$ (options are cheap; buying them doesn't pay a volatility surcharge).
  2. **Expiry Window**: **Next-month contract (20 to 55 DTE)**. *Never buy weekly lottery tickets on single stocks.*
  3. **Moneyness**: Deep/near ITM strike with **Delta $0.55$ to $0.75$**. ITM options have mostly intrinsic value, dramatically slowing daily theta bleed.
  4. **Technical Confirmation**:
     - Bullish Call: Price $>$ SMA20 $>$ SMA50, RSI between $50\text{--}74$, ADX $\ge 18$ or Momentum $\ge 3\%$.
     - Bearish Put: Price $<$ SMA20 $<$ SMA50, RSI between $26\text{--}50$, ADX $\ge 18$ or Momentum $\le -3\%$.
  5. **Auto-Debit Spread Switch**: If IV Rank is between $22\%$ and $32\%$, the engine automatically finances the trade by converting it to a **Debit Spread** (selling an OTM 30-delta option against the long leg).
  6. **Exit Discipline**:
     - Take Profit: $+50\%$ on option premium.
     - Stop Loss: $-40\%$ on option premium.
     - Time Stop: Mandatory close when contract reaches $7\text{--}10\text{ DTE}$ regardless of P&L.

---

## 3. System Architecture

OptionDesk is structured into clean, modular layers:

```
optiondesk/
├── config.yaml          # Sizing, thresholds, broker credentials, alerts
├── run.py               # Unified CLI: serve, scan, check, rules, test-alert
├── tests/               # 17 automated unit tests covering Black-Scholes, greeks, payoffs
└── optiondesk/
    ├── quant.py         # Black-Scholes pricing, greeks, Newton-Raphson IV solver, POP
    ├── volatility.py    # Rolling HV, 365-day IV Rank, IV Percentile, term structure
    ├── indicators.py    # SMA, EMA, RSI, ATR, Wilder's ADX, momentum
    ├── model.py         # OptionChain, OptionQuote, Leg, TradePlan, Signal dataclasses
    ├── strategies.py    # Analytical payoff analyzer, delta-based strike selection, margin est.
    ├── store.py         # SQLite database persistence & alert de-duplication
    ├── alerts.py        # Telegram formatting, digests, console fallbacks
    ├── app.py           # FastAPI server (API endpoints + WebSocket/cron scheduler)
    ├── data/
    │   ├── base.py      # DataProvider interface & expiry date calculators
    │   ├── angel.py     # Angel One SmartAPI client + cached ScripMaster (141k instruments)
    │   └── replay.py    # Deterministic market simulator with volatility smile & skew
    ├── signals/
    │   ├── rules.py     # Trend strength, liquidity filters, capital risk sizing
    │   ├── nifty_spreads.py # Hedged index spread constructor
    │   ├── stock_options.py # Next-month stock buying constructor
    │   └── engine.py    # Multi-underlying scan orchestration
    └── static/
        ├── index.html   # Single-page terminal UI
        ├── styles.css   # Dark quantitative trading layout
        └── app.js       # Live charts, interactive strategy lab, chain inspector
```

---

## 4. Connecting Angel One SmartAPI

OptionDesk connects to Angel One using their official REST API endpoints:
- **Authentication**: `POST /rest/auth/angelbroking/user/v1/loginByPassword` with Client Code, MPIN/Password, and automated TOTP generation via `pyotp`.
- **Option Greeks**: `POST /rest/secure/angelbroking/marketData/v1/optionGreek`
- **Quotes**: `POST /rest/secure/angelbroking/market/v1/quote/`
- **Historical Data**: `POST /rest/secure/angelbroking/historical/v1/getCandleData`
- **Instruments**: Downloads and caches `OpenAPIScripMaster.json` locally (~33 MB).

### Setup Instructions:
1. Register for an API key at [smartapi.angelone.in](https://smartapi.angelone.in).
2. Enable TOTP in your Angel One account to obtain your TOTP QR/secret string.
3. Add credentials to `config.yaml` or set environment variables:

```bash
export ANGEL_API_KEY="your_api_key"
export ANGEL_CLIENT_CODE="your_client_code"
export ANGEL_PIN="your_trading_pin"
export ANGEL_TOTP_SECRET="your_totp_secret_key"
```

When credentials are set, OptionDesk automatically switches to the live `angel` provider. Without credentials, it runs seamlessly in `replay` mode using realistic simulated data.

---

## 5. Setting Up Live Telegram Alerts

To receive real-time notifications on your phone whenever a high-scoring setup fires:

1. Open Telegram and search for `@BotFather`. Create a new bot and copy the **HTTP API Token**.
2. Start a chat with your bot, then get your `chat_id` (via `@userinfobot` or `api.telegram.org/bot<token>/getUpdates`).
3. Set in `config.yaml` or via environment variables:

```bash
export TELEGRAM_BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"
export TELEGRAM_CHAT_ID="987654321"
```

4. Test your notification setup:
```bash
python run.py test-alert
```

---

## 6. How to Run OptionDesk

### 1. Run a One-Shot Market Scan from Terminal:
```bash
python run.py scan
```
Output displays ranked trade signals, exact strikes, net credit/cost, POP, maximum loss, breakevens, and exit rules.

### 2. Launch the Live Web Dashboard & Scanning Daemon:
```bash
python run.py serve --port 8080
```
Open `http://localhost:8080` in your browser. The dashboard provides:
- **Signals Tab**: Live cards for all qualified trades with risk-reward metrics and payoff curves.
- **Nifty Chain Tab**: Full option chain with Call/Put open interest, greeks ($\Delta, \Gamma, \Theta, \nu$), IV, and ATM markers.
- **Stock Scanner Tab**: Master universe table showing spot, DTE, ATM IV, IV Rank, trend status, and action verdicts.
- **Strategy Lab**: Interactive custom multi-leg payoff graph and greeks builder.
- **Rules & Settings**: Live sliders to tune risk budgets, delta targets, and Telegram parameters.

### 3. Check System Health & Broker Status:
```bash
python run.py check
```

### 4. Review Mechanical Strategy Rules:
```bash
python run.py rules
```

### 5. Run the Automated Test Suite:
```bash
pytest -v
```
Verifies Black-Scholes pricing, Put-Call parity, IV root-finders, analytical breakeven solutions, and API route endpoints.
