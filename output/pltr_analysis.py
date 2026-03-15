import pandas as pd
import io
import requests
from datetime import datetime, timedelta

# 获取数据
url = 'https://stooq.com/q/d/l/?s=pltr.us&i=d'
response = requests.get(url)
data = response.text

# 解析CSV数据
df = pd.read_csv(io.StringIO(data))

# 确保日期列是datetime类型
df['Date'] = pd.to_datetime(df['Date'])

# 按日期降序排序
df = df.sort_values('Date', ascending=False)

# 获取最近30天的数据
today = datetime.now()
thirty_days_ago = today - timedelta(days=30)

# 由于数据可能不是最新的，我们取最后30行
recent_30_days = df.head(30)

print("Palantir(PLTR) 最近30天股价走势分析")
print("=" * 60)
print(f"分析日期范围: {recent_30_days['Date'].iloc[-1].strftime('%Y-%m-%d')} 到 {recent_30_days['Date'].iloc[0].strftime('%Y-%m-%d')}")
print(f"数据点数量: {len(recent_30_days)}")
print()

# 基本统计信息
print("基本统计信息:")
print(f"开盘价范围: ${recent_30_days['Open'].min():.2f} - ${recent_30_days['Open'].max():.2f}")
print(f"最高价范围: ${recent_30_days['High'].min():.2f} - ${recent_30_days['High'].max():.2f}")
print(f"最低价范围: ${recent_30_days['Low'].min():.2f} - ${recent_30_days['Low'].max():.2f}")
print(f"收盘价范围: ${recent_30_days['Close'].min():.2f} - ${recent_30_days['Close'].max():.2f}")
print()

# 价格变化分析
start_price = recent_30_days['Close'].iloc[-1]  # 30天前的收盘价
end_price = recent_30_days['Close'].iloc[0]     # 最新的收盘价
price_change = end_price - start_price
percent_change = (price_change / start_price) * 100

print("价格变化分析:")
print(f"起始价格 (30天前): ${start_price:.2f}")
print(f"当前价格 (最新): ${end_price:.2f}")
print(f"价格变化: ${price_change:.2f}")
print(f"百分比变化: {percent_change:.2f}%")
print()

# 波动性分析
daily_returns = recent_30_days['Close'].pct_change().dropna()
volatility = daily_returns.std() * (252 ** 0.5)  # 年化波动率

print("波动性分析:")
print(f"平均日收益率: {daily_returns.mean()*100:.2f}%")
print(f"日收益率标准差: {daily_returns.std()*100:.2f}%")
print(f"年化波动率: {volatility*100:.2f}%")
print()

# 成交量分析
print("成交量分析:")
print(f"平均日成交量: {recent_30_days['Volume'].mean():,.0f}")
print(f"最高日成交量: {recent_30_days['Volume'].max():,.0f}")
print(f"最低日成交量: {recent_30_days['Volume'].min():,.0f}")
print()

# 关键价格水平
print("关键价格水平:")
print(f"30天最高价: ${recent_30_days['High'].max():.2f} (日期: {recent_30_days.loc[recent_30_days['High'].idxmax(), 'Date'].strftime('%Y-%m-%d')})")
print(f"30天最低价: ${recent_30_days['Low'].min():.2f} (日期: {recent_30_days.loc[recent_30_days['Low'].idxmin(), 'Date'].strftime('%Y-%m-%d')})")
print(f"30天平均收盘价: ${recent_30_days['Close'].mean():.2f}")
print()

# 趋势分析
if price_change > 0:
    trend = "上涨"
elif price_change < 0:
    trend = "下跌"
else:
    trend = "持平"

print("趋势分析:")
print(f"总体趋势: {trend}")
print(f"上涨天数: {(daily_returns > 0).sum()}")
print(f"下跌天数: {(daily_returns < 0).sum()}")
print(f"持平天数: {(daily_returns == 0).sum()}")

# 显示最近10天的数据
print("\n最近10天交易数据:")
print("=" * 80)
print(f"{'日期':<12} {'开盘价':<10} {'最高价':<10} {'最低价':<10} {'收盘价':<10} {'成交量':<15} {'日涨跌幅':<10}")
print("-" * 80)

for i in range(min(10, len(recent_30_days))):
    row = recent_30_days.iloc[i]
    daily_return = ((row['Close'] - row['Open']) / row['Open']) * 100 if row['Open'] != 0 else 0
    print(f"{row['Date'].strftime('%Y-%m-%d'):<12} ${row['Open']:<9.2f} ${row['High']:<9.2f} ${row['Low']:<9.2f} ${row['Close']:<9.2f} {row['Volume']:<15,.0f} {daily_return:<9.2f}%")