
#property strict
#property version "1.00"
#property description "Forex Scalper EA - EMA crossover, ATR stops and risk controls"

#include <Trade/Trade.mqh>

CTrade trade;

// ===== SAFETY SETTINGS =====
input bool   DemoOnly             = true;
input double RiskPercent          = 1.0;
input double DailyLossLimitPct    = 3.0;
input bool   AllowNewTrades       = true;

// ===== STRATEGY SETTINGS =====
input ENUM_TIMEFRAMES SignalTF    = PERIOD_M5;
input int    FastEMA              = 9;
input int    SlowEMA              = 21;
input int    ATRPeriod            = 14;
input double StopATRMultiplier    = 1.5;
input double TargetATRMultiplier  = 2.0;

// ===== EXECUTION SETTINGS =====
input int    MaxSpreadPoints      = 30;
input int    SlippagePoints       = 10;
input ulong  MagicNumber          = 26101401;

// Indicator handles
int fastHandle = INVALID_HANDLE;
int slowHandle = INVALID_HANDLE;
int atrHandle  = INVALID_HANDLE;

datetime lastBarTime = 0;
string dailyKey = "";

//+------------------------------------------------------------------+
//| Initialize                                                       |
//+------------------------------------------------------------------+
int OnInit()
{
   if(FastEMA < 1 || SlowEMA < 2 || FastEMA >= SlowEMA ||
      ATRPeriod < 1 || RiskPercent <= 0 ||
      DailyLossLimitPct <= 0 ||
      StopATRMultiplier <= 0 || TargetATRMultiplier <= 0)
   {
      Print("Invalid strategy or risk settings.");
      return INIT_PARAMETERS_INCORRECT;
   }

   if(DemoOnly &&
      AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO)
   {
      Print("SAFETY STOP: DemoOnly is enabled. Use a DEMO account.");
      return INIT_FAILED;
   }

   fastHandle = iMA(_Symbol, SignalTF, FastEMA, 0, MODE_EMA, PRICE_CLOSE);
   slowHandle = iMA(_Symbol, SignalTF, SlowEMA, 0, MODE_EMA, PRICE_CLOSE);
   atrHandle  = iATR(_Symbol, SignalTF, ATRPeriod);

   if(fastHandle == INVALID_HANDLE ||
      slowHandle == INVALID_HANDLE ||
      atrHandle == INVALID_HANDLE)
   {
      Print("Could not create indicators.");
      return INIT_FAILED;
   }

   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(SlippagePoints);
   trade.SetTypeFillingBySymbol(_Symbol);

   Print("Forex Scalper EA initialized on ", _Symbol,
         ". New trades allowed only when all safety checks pass.");

   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Release indicator resources                                      |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   if(fastHandle != INVALID_HANDLE) IndicatorRelease(fastHandle);
   if(slowHandle != INVALID_HANDLE) IndicatorRelease(slowHandle);
   if(atrHandle  != INVALID_HANDLE) IndicatorRelease(atrHandle);
}

//+------------------------------------------------------------------+
//| Persistent daily equity baseline                                 |
//+------------------------------------------------------------------+
bool GetDailyBaseline(double &baseline)
{
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);

   string dateText = StringFormat("%04d%02d%02d",
                                  dt.year, dt.mon, dt.day);

   string key = StringFormat("FSE_%I64d_%s",
                             AccountInfoInteger(ACCOUNT_LOGIN),
                             dateText);

   if(!GlobalVariableCheck(key))
   {
      if(!GlobalVariableSet(key, AccountInfoDouble(ACCOUNT_EQUITY)))
         return false;
   }

   baseline = GlobalVariableGet(key);
   return (baseline > 0);
}

//+------------------------------------------------------------------+
//| Daily drawdown safety check                                      |
//+------------------------------------------------------------------+
bool DailyLossLimitReached()
{
   double baseline = 0;

   if(!GetDailyBaseline(baseline))
   {
      Print("Cannot read daily baseline. Blocking new trades.");
      return true;
   }

   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double lossPct = 100.0 * (baseline - equity) / baseline;

   if(lossPct >= DailyLossLimitPct)
   {
      Print("DAILY LOSS LIMIT REACHED: ", DoubleToString(lossPct, 2),
            "%. No new trades will be opened today.");
      return true;
   }

   return false;
}

//+------------------------------------------------------------------+
//| Detect a new candle                                              |
//+------------------------------------------------------------------+
bool IsNewBar()
{
   datetime barTime = iTime(_Symbol, SignalTF, 0);

   if(barTime <= 0 || barTime == lastBarTime)
      return false;

   lastBarTime = barTime;
   return true;
}

//+------------------------------------------------------------------+
//| Check whether this EA already has a position on this symbol     |
//+------------------------------------------------------------------+
bool HasOurPosition()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);

      if(ticket == 0)
         continue;

      if(PositionGetString(POSITION_SYMBOL) == _Symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == MagicNumber)
      {
         return true;
      }
   }

   return false;
}

//+------------------------------------------------------------------+
//| Calculate risk-based lot size                                    |
//+------------------------------------------------------------------+
double CalculateLots(ENUM_ORDER_TYPE orderType,
                     double entryPrice,
                     double stopPrice)
{
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskMoney = equity * RiskPercent / 100.0;

   double oneLotResult = 0;

   if(!OrderCalcProfit(orderType, _Symbol, 1.0,
                       entryPrice, stopPrice, oneLotResult))
   {
      Print("Lot calculation failed. Error: ", GetLastError());
      return 0;
   }

   double oneLotLoss = MathAbs(oneLotResult);

   if(oneLotLoss <= 0)
      return 0;

   double rawLots = riskMoney / oneLotLoss;

   double minLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double step   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);

   if(step <= 0 || minLot <= 0 || maxLot <= 0)
      return 0;

   // Never round up the calculated risk.
   double lots = MathFloor(rawLots / step + 1e-9) * step;

   if(lots < minLot)
   {
      Print("Trade skipped: calculated volume is below broker minimum. ",
            "No forced minimum lot.");
      return 0;
   }

   lots = MathMin(lots, maxLot);

   // Normalize volume to the broker's volume step.
   int volumeDigits = 0;
   double checkStep = step;

   while(volumeDigits < 8 &&
         MathAbs(checkStep - MathRound(checkStep)) > 1e-8)
   {
      checkStep *= 10.0;
      volumeDigits++;
   }

   return NormalizeDouble(lots, volumeDigits);
}

//+------------------------------------------------------------------+
//| Main trading logic                                               |
//+------------------------------------------------------------------+
void OnTick()
{
   if(!AllowNewTrades)
      return;

   if(DemoOnly &&
      AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO)
      return;

   // Analyze once per new candle, using completed candles only.
   if(!IsNewBar())
      return;

   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ||
      !MQLInfoInteger(MQL_TRADE_ALLOWED) ||
      !AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))
   {
      Print("Trading disabled by terminal, EA, or account.");
      return;
   }

   if(DailyLossLimitReached())
      return;

   if(HasOurPosition())
      return;

   MqlTick tick;

   if(!SymbolInfoTick(_Symbol, tick))
      return;

   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);

   if(point <= 0)
      return;

   double spreadPoints = (tick.ask - tick.bid) / point;

   if(spreadPoints > MaxSpreadPoints)
   {
      Print("Trade skipped: spread is too high: ",
            DoubleToString(spreadPoints, 1), " points.");
      return;
   }

   double fast[3], slow[3], atr[3];

   ArraySetAsSeries(fast, true);
   ArraySetAsSeries(slow, true);
   ArraySetAsSeries(atr, true);

   if(CopyBuffer(fastHandle, 0, 0, 3, fast) != 3 ||
      CopyBuffer(slowHandle, 0, 0, 3, slow) != 3 ||
      CopyBuffer(atrHandle, 0, 0, 3, atr) != 3)
   {
      Print("Waiting for enough indicator data.");
      return;
   }

   // Index 1 = most recently completed candle.
   // Index 2 = the candle before it.
   bool buySignal  = (fast[2] <= slow[2] && fast[1] > slow[1]);
   bool sellSignal = (fast[2] >= slow[2] && fast[1] < slow[1]);

   if(!buySignal && !sellSignal)
      return;

   double currentATR = atr[1];

   if(currentATR <= 0)
      return;

   ENUM_ORDER_TYPE orderType;
   double entry, sl, tp;

   if(buySignal)
   {
      orderType = ORDER_TYPE_BUY;
      entry = tick.ask;
      sl = entry - currentATR * StopATRMultiplier;
      tp = entry + currentATR * TargetATRMultiplier;
   }
   else
   {
      orderType = ORDER_TYPE_SELL;
      entry = tick.bid;
      sl = entry + currentATR * StopATRMultiplier;
      tp = entry - currentATR * TargetATRMultiplier;
   }

   // Respect broker's minimum stop distance.
   long stopsLevel = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   double minDistance = stopsLevel * point;

   if(buySignal)
   {
      sl = MathMin(sl, tick.bid - minDistance);
      tp = MathMax(tp, tick.ask + minDistance);
   }
   else
   {
      sl = MathMax(sl, tick.ask + minDistance);
      tp = MathMin(tp, tick.bid - minDistance);
   }

   sl = NormalizeDouble(sl, digits);
   tp = NormalizeDouble(tp, digits);

   // Check the lot size against the FINAL stop-loss price.
   double lots = CalculateLots(orderType, entry, sl);

   if(lots <= 0)
      return;

   bool sent = false;

   if(buySignal)
   {
      sent = trade.Buy(lots, _Symbol, 0, sl, tp, "EMA ATR BUY");
   }
   else if(sellSignal)
   {
      sent = trade.Sell(lots, _Symbol, 0, sl, tp, "EMA ATR SELL");
   }

   if(!sent)
   {
      Print("Order failed. Retcode: ", trade.ResultRetcode(),
            " - ", trade.ResultRetcodeDescription());
      return;
   }

   Print(buySignal ? "BUY order request completed." :
                     "SELL order request completed.",
         " Lots: ", DoubleToString(lots, 2),
         " SL: ", DoubleToString(sl, digits),
         " TP: ", DoubleToString(tp, digits),
         ". Check broker execution and deal confirmation.");
}
