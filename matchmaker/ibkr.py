import re
import pandas as pd
import numpy as np
import io
from matchmaker.trade import normalize_trades
import matchmaker.actions as actions
import matchmaker.position as position

def dataframe_from_prefixed_lines(line_dict: dict, prefix: str) -> pd.DataFrame:
    """
    Create a DataFrame from lines with the same prefix. Expects a dictionary of lines parsed from a CSV file.
    """
    if prefix not in line_dict:
        return pd.DataFrame()
    file_data = io.StringIO(''.join(line_dict[prefix]))
    return pd.read_csv(file_data)

# Parses the CSV into a dictionary of lines with the same prefix
def parse_csv_into_prefixed_lines(file: io.BytesIO) -> dict:
    """
    Parse the CSV into a dictionary of lines
    """
    file.seek(0)
    file = [line.decode('utf-8') for line in file]
    prefix_dict = {}
    for line in file:
        key = line.split(',', 1)[0]
        if key not in prefix_dict:
            prefix_dict[key] = []
        prefix_dict[key].append(line)
    return prefix_dict

def convert_option_names(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert raw option names into detailed option fields
    """
    if 'Option Name' not in df.columns:
        return df
    # Vectorized parsing of option names
    option_mask = df['Option Name'].notna() 
    option_parts = df.loc[option_mask, 'Symbol'].str.split(' ', expand=True)
    if not option_parts.empty:
        # Splits the option name into symbol, expiration date, strike price and put/call
        # Example option name: CELH 20SEP24 40 P
        df.loc[option_mask, 'Option Name'] = df.loc[option_mask, 'Symbol']
        df.loc[option_mask, 'Expiration'] = option_parts[1]
        df.loc[option_mask, 'Strike'] = option_parts[2]
        df.loc[option_mask, 'Option Type'] = option_parts[3].map({'P': 'Put', 'C': 'Call'})
        df.loc[option_mask, 'Display Suffix'] = ' ' + option_parts[1] + ' ' + option_parts[2] + ' ' + df.loc[option_mask, 'Option Type']
        df.loc[option_mask, 'Symbol'] = option_parts[0]
    return df
    
# Import trades from IBKR format
# First line is the headers: Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code
# Column	Descriptions
# Trades	The trade number.
# Header	Header record contains the report title and the date and time of the report.
# Asset Category	The asset category of the instrument. Possible values are: "Stocks", "Options", "Futures", "FuturesOptions
# Symbol	    The symbol of the instrument you traded.
# Date/Time	The date and the time of the execution.
# Quantity	The number of units for the transaction.
# T. Price	The transaction price.
# C. Price	The closing price of the instrument.
# Proceeds	Calculated by mulitplying the quantity and the transaction price. The proceeds figure will be negative for buys and positive for sales.
# Comm/Fee	The total amount of commission and fees for the transaction.
# Basis	    The basis of an opening trade is the inverse of proceeds plus commission and tax amount. For closing trades, the basis is the basis of the opening trade.

# Data begins on the second line
# Example line: Trades,Data,Order,Stocks,CZK,CEZ,"2023-08-03, 08:44:03",250,954,960,-238500,-763.2,239263.2,0,1500,O
def import_trades(file: io.BytesIO) -> pd.DataFrame:
    """
    Import the trades section from IBKR format.
    """
    df = dataframe_from_prefixed_lines(file, 'Trades')
    if df.empty:
        df = pd.DataFrame(columns=['Trades', 'Header', 'DataDiscriminator', 'Asset Category', 'Currency', 'Symbol', 'Date/Time', 'Quantity', 'T. Price', 'C. Price', 'Proceeds', 'Comm/Fee', 'Basis', 'Realized P/L', 'MTM P/L', 'Code'])
    df = df[(df['Trades'] == 'Trades') & (df['Header'] == 'Data') & (df['DataDiscriminator'] == 'Order') & ((df['Asset Category'] == 'Stocks') | (df['Asset Category'] == 'Equity and Index Options'))]
    df['Date/Time'] = pd.to_datetime(df['Date/Time'], format='%Y-%m-%d, %H:%M:%S')
    df['Quantity'] = pd.to_numeric(df['Quantity'].astype(str).str.replace(',', ''), errors='coerce')
    df['Option Name'] = df[df['Asset Category'] == 'Equity and Index Options']['Symbol']
    df['Display Suffix'] = ''
    df = convert_option_names(df)
    df.drop(columns=['Trades', 'Header', 'DataDiscriminator', 'Asset Category',], inplace=True)
    return normalize_trades(df)

def import_corporate_actions(file: io.BytesIO) -> pd.DataFrame:
    """
    Import corporate actions from IBKR format.
    """
    df = dataframe_from_prefixed_lines(file, 'Corporate Actions')
    if df.empty:
        df = pd.DataFrame(columns=['Corporate Actions', 'Header', 'Asset Category', 'Currency', 'Report Date', 'Date/Time', 'Description', 'Quantity', 'Proceeds', 'Value', 'Realized P/L', 'Action', 'Symbol', 'Ratio', 'Code', 'Target'])
    df = df[df['Asset Category'] == 'Stocks']
    df.drop(columns=['Corporate Actions', 'Header', 'Asset Category'], inplace=True)
    
    def parse_action_text(text: str) -> tuple:
        action, symbol, ratio, target = parse_split_text(text)
        if not action:
            action, symbol, ratio, target = parse_spinoff_text(text)
        if not action:
            action, symbol, ratio, target = parse_acquisition_text(text)
        if not action:
            match = re.search(r'^(\w+)\(\w+\)', text)
            if match:
                action, symbol, ratio, target = 'Unknown', match.group(1), 0.0, None
        if not action and 'Dividend' in text:
            action, symbol, ratio, target = 'Dividend', None, None, None
        if not action:
            action, symbol, ratio, target = 'Unknown', None, None, None
        return action, symbol, ratio, target
    
    def parse_split_text(text: str) -> tuple:
        match = re.search(r'([\w\.]+)\(\w+\) Split (\d+) for (\d+)', text, re.IGNORECASE)
        if match:
            ratio = float(match.group(3)) / float(match.group(2))
            return 'Split', match.group(1), ratio, None
        return None, None, None, None
    
    def parse_spinoff_text(text: str) -> tuple:
        match = re.search(r'^(\w+)\(\w+\) Spinoff\s+(\d+) for (\d+) \((\w+),.+\)', text, re.IGNORECASE)
        if match:
            ratio = float(match.group(2)) / float(match.group(3))
            return 'Spinoff', match.group(4), ratio, None
        return None, None, None, None
    
    def parse_acquisition_text(text: str) -> tuple:
        # Stock bought by another: ATVI(US00507V1098) Merged(Acquisition) FOR USD 95.00 PER SHARE
        match = re.search(r'^(\w+)\(\w+\) Merged\(Acquisition\) FOR (\w+) (\d+\.\d+) PER SHARE', text, re.IGNORECASE)
        if match:
            ratio = float(match.group(3))
            return 'Acquisition', match.group(1), ratio, None
        # Converted to other stock: MRO(US5658491064) Merged(Acquisition) WITH US20825C1045 255 for 1000 (COP, CONOCOPHILLIPS, US20825C1045)
        match = re.search(r'^(\w+)\(\w+\) Merged\(Acquisition\) WITH (\w+) (\d+) for (\d+) \((\w+),', text, re.IGNORECASE)
        if match:
            ratio = float(match.group(4)) / float(match.group(3))
            return 'Acquisition', match.group(5), ratio, match.group(1)
        return None, None, None, None
    
    df['Quantity'] = pd.to_numeric(df['Quantity'].astype(str).str.replace(',', ''), errors='coerce')
    df['Date/Time'] = pd.to_datetime(df['Date/Time'], format='%Y-%m-%d, %H:%M:%S')
    if not df.empty:
        df[['Action', 'Symbol', 'Ratio', 'Target']] = df['Description'].apply(lambda x: pd.Series(parse_action_text(x)))
    df.drop(columns=['Code'], inplace=True)
    df = actions.convert_action_columns(df)
    return df

def import_open_positions(file: io.BytesIO, date_from: pd.Timestamp, date_to: pd.Timestamp) -> pd.DataFrame:
    """
    Import open positions from IBKR format.
    """
    # Old format that doesn't reflect symbol changes
    df = dataframe_from_prefixed_lines(file, 'Mark-to-Market Performance Summary')
    if df.empty:
        # Mark-to-Market Performance Summary,Header,Asset Category,Symbol,Prior Quantity,Current Quantity,Prior Price,
        # Current Price,Mark-to-Market P/L Position,Mark-to-Market P/L Transaction,Mark-to-Market P/L Commissions,Mark-to-Market P/L Other,Mark-to-Market P/L Total,Code
        df = pd.DataFrame(columns=['Mark-to-Market Performance Summary', 'Header', 'Asset Category', 'Symbol', 'Prior Quantity', 'Current Quantity', 'Prior Price', 'Current Price',
                                'Mark-to-Market P/L Position','Mark-to-Market P/L Transaction','Mark-to-Market P/L Commissions','Mark-to-Market P/L Other','Mark-to-Market P/L Total','Code'])
    df = df[(df['Asset Category'] == 'Stocks') | (df['Asset Category'] == 'Equity and Index Options')]
    df['Option Name'] = df[df['Asset Category'] == 'Equity and Index Options']['Symbol']
    df['Display Suffix'] = ''
    df = convert_option_names(df)
    df.drop(columns=['Mark-to-Market Performance Summary', 'Header', 'Asset Category', 'Code'], inplace=True)
    df['Prior Date'] = date_from
    df['Current Date'] = date_to
    return position.convert_position_history_columns(df)

def import_transfers(file: io.BytesIO) -> pd.DataFrame:
    """
    Import transfers from IBKR format.
    """
    df = dataframe_from_prefixed_lines(file, 'Transfers')
    if df.empty:
        # Transfers,Header,Asset Category,,Currency,Symbol,Date,Type,Direction,Xfer Company,Xfer Account,Qty,Xfer Price,Market Value,Realized P/L,Cash Amount,Code
        df = pd.DataFrame(columns=['Transfers', 'Header', 'Asset Category', 'Currency', 'Symbol', 'Date', 'Type', 'Direction', 'Xfer Company', 'Xfer Account', 'Qty', 'Xfer Price', 'Market Value', 'Realized P/L', 'Cash Amount', 'Code'])
    df = df[df['Asset Category'] == 'Stocks']
    df['Date/Time'] = pd.to_datetime(df['Date'], format='%Y-%m-%d')
    df['Year'] = df['Date/Time'].dt.year
    df['Action'] = 'Transfer'
    df['Quantity'] = pd.to_numeric(df['Qty'].astype(str).str.replace(',', ''), errors='coerce')
    df['Proceeds'] = pd.to_numeric(df['Market Value'].astype(str).str.replace(',', ''), errors='coerce')
    df['Comm/Fee'] = 0
    df['Basis'] = 0
    df['Realized P/L'] = 0
    df['MTM P/L'] = 0
    df['T. Price'] = 0
    df['C. Price'] = 0
    df['Target'] = df['Xfer Account']
    df.drop(columns=['Transfers', 'Header', 'Asset Category', 'Date', 'Type', 'Direction', 'Xfer Company', 'Xfer Account', 'Qty', 'Xfer Price', 'Market Value', 'Cash Amount', 'Code'], inplace=True)
    return normalize_trades(df)

def generate_transfers_from_actions(actions: pd.DataFrame) -> pd.DataFrame:
    """
    Generate asset transfers from a list of corporate actions.
    """
    spinoffs = actions[(actions['Action'] == 'Spinoff') | (actions['Action'] == 'Acquisition')]
    transfers = pd.DataFrame()
    for index, spinoff in spinoffs.iterrows():
        transfer = {
            'Date/Time': spinoff['Date/Time'] - pd.Timedelta(seconds=1),
            'Currency': spinoff['Currency'],
            'Symbol': spinoff['Symbol'],
            'Quantity': spinoff['Quantity'],
            'Proceeds': spinoff['Proceeds'],
            'Comm/Fee': 0,
            'Basis': 0,
            'Realized P/L': spinoff['Realized P/L'],
            'MTM P/L': 0,
            'T. Price': spinoff['Proceeds'] / abs(spinoff['Quantity'])  if spinoff['Quantity'] != 0 else 0,
            'C. Price': 0,
            'Action': 'Open' if spinoff['Quantity'] >= 0 else 'Close',
            'Type': spinoff['Action'],
        }
        transfers = pd.concat([transfers, pd.DataFrame([transfer])], ignore_index=True)
    return normalize_trades(transfers)

# @st.cache_data()
def import_activity_statement(file: io.BytesIO) -> tuple:
    """
    Import the entire IBKR activity statement, returning dataframes for trades, actions and open positions.
    """
    file.seek(0)
    # 2nd line: Statement,Data,Title,Activity Statement
    # 3rd line: Statement,Data,Period,"April 13, 2020 - April 12, 2021"
    while line := file.readline().decode('utf-8'):
        if line.startswith('Statement,Data,Title,Activity '):
            break
    match_period = re.match('Statement,Data,Period,"(.+) - (.+)"', file.readline().decode('utf-8'))
    if not match_period:
        raise Exception('No period in IBKR Activity Statement')
    lines = parse_csv_into_prefixed_lines(file)

    # Convert to from and to dates
    from_date = pd.to_datetime(match_period.group(1), format='%B %d, %Y')
    to_date = pd.to_datetime(match_period.group(2), format='%B %d, %Y')    
    trades = import_trades(lines)
    actions = import_corporate_actions(lines)
    open_positions = import_open_positions(lines, from_date, to_date)
    transfers = import_transfers(lines)
    transfers = pd.concat([transfers, generate_transfers_from_actions(actions)])
    trades = pd.concat([trades, transfers])
    # Fill in account info into trades so open positions can be computed and verified per account
    account_info = dataframe_from_prefixed_lines(lines, 'Account Information')
    account = account_info[account_info['Field Name'] == 'Account'].iloc[0]['Field Value']
    account = re.match(r'U\d+', account).group(0)
    if 'Account' not in trades.columns:
        trades['Account'] = account
    trades['Account'].fillna(account, inplace=True)
    open_positions['Account'] = account

    imported = pd.DataFrame({
        'Account': [account],
        'From': [from_date],
        'To': [to_date],
        'Trade Count': [len(trades)],
    })

    return imported, trades, actions, open_positions