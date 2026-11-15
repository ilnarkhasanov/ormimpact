SELECT date_trunc('month', created) AS m, sum(total) FROM shop_order GROUP BY 1;
SELECT name FROM shop_customer;
