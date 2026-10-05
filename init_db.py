import pymysql

try:
    # Connect to XAMPP MySQL without specifying a DB, using default root/no-password
    conn = pymysql.connect(host='127.0.0.1', user='root', password='')
    cursor = conn.cursor()
    # Create database if it doesn't exist
    cursor.execute("CREATE DATABASE IF NOT EXISTS onebridge_scraper CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
    conn.commit()
    print("Database onebridge_scraper created or already exists.")
    cursor.close()
    conn.close()
except Exception as e:
    print(f"Error connecting to MySQL: {e}")
