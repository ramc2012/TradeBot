import os, psycopg2, pandas as pd
c = psycopg2.connect(os.environ["VANGUARD_DATABASE_URL"])
q = """select column_name, data_type from information_schema.columns
       where table_name='underlying_spot_candles' order by ordinal_position"""
print(pd.read_sql(q, c).to_string())
print(pd.read_sql("""select interval, count(*) n, min(time) mn, max(time) mx
                     from underlying_spot_candles
                     where underlying in ('SBIN','AUBANK','FEDERALBNK','ICICIBANK')
                     group by 1 order by 2 desc""", c).to_string())
