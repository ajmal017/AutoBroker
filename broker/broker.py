import json
from ib_insync import *
import pandas as pd
from pandas_datareader import data as pd_data
from typing import Set, Dict
from datetime import datetime, timedelta

SETTINGS_PATH = 'settings\\settings.json'
TICKERS_PATH = 'settings\\tickers.xlsx'
CACHE_DIR = 'cache'


# extend json.JSONEncoder to handle pandas dataframes
# Credit: https://stackoverflow.com/questions/33061302/dictionary-of-panda-dataframe-to-json
class JSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if hasattr(obj, 'to_json'):
            return obj.to_json(orient='records')
        return json.JSONEncoder.default(self, obj)


tickers = set()
historical_data = dict()
portfolio = pd.DataFrame(columns=['Sharpe (unadjusted)'])

try:
    settings = json.load(open(SETTINGS_PATH, 'r'))
except Exception as e:
    print('Loading settigs failed')
    print(e)

try:
    ib = IB()
    ib.connect(settings['TWS_ip'], settings['TWS_port'], settings['TWS_id'])
except Exception as e:
    print('Connecting to TWS failed')
    print(e)


def get_tickers(path : str = TICKERS_PATH) -> Set[str]:
    """
    Get ticker symbols from an excel sheet. Return ticker symbols as a set
    of strings

    path -- path to excel sheet
    """
    sheet_data = pd.read_excel(path, skipna=True)

    ticker_series = sheet_data.iloc[:, 0].dropna()

    global tickers
    tickers = set(ticker_series)

    # if tickers are not in portfolio, add them
    global portfolio
    missing_tickers = list(tickers - set(portfolio.index))
    portfolio = portfolio.reindex(portfolio.index.union(missing_tickers))

    return tickers


def get_historical_data(symbols: Set[str] = None) -> Dict[str, pd.DataFrame]:
    """ 
    Pull weekly historical data for all ticker symbols going back 52 
    weeks from current date.
    Return data in a dict of ticker symbols mapped to pandas DataFrame 
    objects. DataFrames are indexed 1 to 52 with 52 being the most 
    recent week, and contain the following columns: open, high, low, 
    close and volume.

    symbols -- Set of ticker symbols (example: {'TSLA', 'MSFT'})
    """

    if symbols is None:
        global tickers
        symbols = tickers
    
    # Set start_date to Monday of 52 weeks ago, end_date to last friday
    cur_weekday = datetime.now().weekday()
    start_date = datetime.now() - timedelta(weeks=52, days=cur_weekday)
    end_date = datetime.now() - timedelta(days=3+cur_weekday)

    # Dates of monday-friday of each week going back 52 weeks
    week_dates = list()
    for i in range(52):
        monday = datetime.now() - timedelta(weeks=52-i, days=cur_weekday)
        week = tuple((monday + timedelta(days=j)).strftime('%Y-%m-%d')
                     for j in range(5))
        week_dates.append(week)

    # Read cache file
    with open(CACHE_DIR + '/historical_data.json', 'r') as historical_cache:
        cached_data = json.load(historical_cache)

        for ticker, data in cached_data.items():
            cached_data[ticker] = pd.read_json(data)

    # Container for weekly historical data
    global historical_data
    historical_data = {
        ticker: pd.DataFrame(
            columns=['weekof', 'open', 'high', 'low', 'close', 'volume']
        )
        for ticker in symbols
    }

    # if tickers are missing in cached data, get iex data
    missing_tickers = symbols - set(cached_data.keys())
    
    if len(missing_tickers) > 0:
        data_pull = pd_data.DataReader(
            list(missing_tickers), 'iex', start_date, end_date)
    
        for i, week in enumerate(week_dates):
            # Get historical data rows for each day of the week 
            # (if there is data for that day)
            week_series = [
                data_pull.loc[day, :]
                for day in week
                if day in data_pull.index
            ]

            # Populate weekly data for each ticker
            for ticker in missing_tickers:
                # Place weekly average for each column indexed by week number
                historical_data[ticker].loc[i + 1] = [
                    # Week date is first day of week
                    week[0],

                    # Week open is open of first day
                    week_series[0].loc['open'].loc[ticker],

                    # Week high is highest high of the week
                    max(day.loc['high'].loc[ticker] for day in week_series),

                    # Week low is lowest low of the week
                    min(day.loc['low'].loc[ticker] for day in week_series),

                    # Week close is close of last day
                    week_series[-1].loc['close'].loc[ticker],

                    # Week volume is last volume of week
                    week_series[-1].loc['volume'].loc[ticker]
                ]
    
    # Get historical data from cached data
    for ticker in {t for t, d in historical_data.items() if d.empty}:
        df = cached_data[ticker]

        for i, week in enumerate(week_dates):
            week_from_cache = df.loc[df['weekof'].isin(week)]

            # if there is no data for that week get it from iex
            if week_from_cache.empty:
                data_pull = pd_data.DataReader(ticker, 'iex', week[0], week[-1])

                week_series = [
                    data_pull.loc[day, :]
                    for day in week
                    if day in data_pull.index
                ]

                historical_data[ticker].loc[i + 1] = [
                    # Week date is first day of week
                    week[0],

                    # Week open is open of first day
                    week_series[0].loc['open'],

                    # Week high is highest high of the week
                    max(day.loc['high'] for day in week_series),

                    # Week low is lowest low of the week
                    min(day.loc['low'] for day in week_series),

                    # Week close is close of last day
                    week_series[-1].loc['close'],

                    # Week volume is last volume of week
                    week_series[-1].loc['volume']
                ]
            else:
                # Use cached data
                historical_data[ticker].loc[i + 1] = week_from_cache.iloc[0]

    # Save historical data to cache
    with open(CACHE_DIR + '/historical_data.json', 'w') as historical_cache:
        json.dump(historical_data, historical_cache, cls=JSONEncoder)

    return historical_data


def _sharpe_single(weekly_change: pd.DataFrame, weeks: int = 52) -> float:
    """
    Internal hepler function

    Calculate sharpe ratio of specified data over specifed number of 
    weeks. Return numeric value.

    weekly_change --  pandas DataFrame containing column 'change'
    weeks -- number of weeks to account in sharpe ratio
    """

    # Get change data of weeks in question
    total_weeks = weekly_change.iloc[:, 0].count()
    change = weekly_change.loc[total_weeks-weeks : total_weeks, 'change']

    # Calculate average change
    average = change.mean(skipna=True)

    # Calculate standard deviation
    deviation = change.std(skipna=True)

    return average / deviation


def _weekly_change(weekly_data: Dict[str, pd.DataFrame]) \
        -> Dict[str, pd.DataFrame]:
    """
    Internal helper function

    Calculate week to week change of close values in weekly data.
    Return data in a dict of ticker symbols mapped to pandas DataFrames.
    DataFrames contain close and change columns where close is week 
    close and change is percent change from previous week

    weekly_data -- dict of ticker symbols mapped to pandas DataFrames 
                   which must contain a close column
    """

    # Get ticker symbols
    tickers = set(weekly_data.keys())

    weekly_change = dict()

    for ticker in tickers:
        # Get close column
        close = weekly_data[ticker].loc[:, 'close']
        # Calculate weekly change
        change = close.pct_change()

        # Combine close and change into single data frame
        df = pd.DataFrame(close)
        df.loc[:, 'change'] = change

        # Assign dataframe to ticker symbol
        weekly_change[ticker] = df

    return weekly_change


def _sharpe_single(weekly_change: pd.DataFrame, weeks: int = 52) -> float:
    """
    Internal helper function

    Calculate sharpe ratio of specified data over specifed number of 
    weeks. Return numeric value.

    weekly_change --  pandas DataFrame containing column 'change'
    weeks -- number of weeks to account in sharpe ratio
    """

    # Get change data of weeks in question
    total_weeks = weekly_change.iloc[:, 0].count()
    change = weekly_change.loc[total_weeks-weeks : total_weeks, 'change']

    # Calculate average change
    average = change.mean(skipna=True)

    # Calculate standard deviation
    deviation = change.std(skipna=True)

    return average / deviation


def sharpe_ratios(weekly_data: Dict[str, pd.DataFrame] = None) \
        -> Dict[str, float]:
    """
    Calculate average sharpe ratio for each ticker.
    Average sharpe ratio is the averege of sharpe ratios calculated over 
    52, 26 and 13 weeks.
    Return dict of ticker symbols mapped to average sharpe values.

    weekly_data -- dict of ticker symbols mapped to pandas DataFrames 
                   which must contain a close column
    """

    if weekly_data is None:
        global historical_data
        weekly_data = historical_data
    
    # Get weekly change
    change = _weekly_change(weekly_data)

    # Get ticker symbols
    tickers = set(change.keys())

    sharpes = dict()

    # if tickers are not in portfolio, add them
    global portfolio
    missing_tickers = list(tickers - set(portfolio.index))
    portfolio = portfolio.reindex(portfolio.index.union(missing_tickers))

    for ticker in tickers:
        data = change[ticker]

        # Calculate sharpe ratios for ticker
        sharpe_52 = _sharpe_single(data, 52)
        sharpe_26 = _sharpe_single(data, 26)
        sharpe_13 = _sharpe_single(data, 13)

        # Assign average of sharpe ratios to ticker
        average = (sharpe_52 + sharpe_26 + sharpe_13) / 3

        sharpes[ticker] = average

        # Update portfolio
        portfolio.loc[ticker]['Sharpe (unadjusted)'] = average

    return sharpes