SELECT c.name, sum(o.total_cents) FROM orders o JOIN customers c ON c.id = o.customer_id GROUP BY c.name;
