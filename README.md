# Peter Grecian - CV
## code to publish my Curriculum Vitae 
Lambda is used to publish web pages via API gateway.  If the website gets more complicated I'll use Flask.

The script "update" is used to zip and push the assets to AWS.  It provides git info on the website which can be used to confirm the state of the live code.  The pattern of use is:
```
./update
test
git commit ...
./update
```
It would be preferable to use github actions, however this is very slow in comparison.
# Comparative Costs
There are many ways of inexpensively publishing a short document such as a CV.  Using an EC2
instance and a webserver, Apache for example, would cost $40 per year for the smallest, least expensive instance, a t4g.nano.  This would come down to $24 per year if payment was made one year advance.  Spot instances are a little cheaper at half the full price, however obviously, availability is not guaranteed.

Serverless methods for low volume demand is much cheaper since the cost is proportional to the number of impressions.  Lambda is $0.20 per million requests, and $0.16 per million 128MB, 100ms invocations.

The 9 cents per GB *data to the internet* charge is unlikely to incur costs, unless the document goes viral.  My CV is about a third of 100k in size, mostly because it contains a mug shot.  The free 100GB per month is three million requests; about once every second.  Thereafter the charge would be $3 per million request, easily dominating the charge per request.  

Cost Anomaly detection would be triggered if a the cost was greater than expected.  The account budget also would alert.  AWS WAF could be used.  This requires API Gateway, Cloudfront or and ALB ($220 per year).  A WAF Web ACL with a single rule is $72 per year and $0.60 per million requests.  API Gateway has throttling, which is typically set to 100 requests per second which is $900 per month.  I've set it to 1 request per second. 

Cloudfront has an "Always Free Tier", of 1TB data transfer to internet and 10 million requests which would be sufficient for this use case.  Usage beyond this is only marginally less expensive than data costs without cloudfront.

Route53 costs $6 per hosted zone per year, $0.40 per million queries.  The top level domain I use, .co.uk, is $9 per year.  Most are much more expensive, only .me.uk ($8) and .link ($5) are cheaper.  

## Always Free Tier
Relevant design information would be the amount of the 400000 Lambda-GB-Second and 1 million requests per month of the always free tier allowance already used, the rate at which it is being used and the expected day of the month on which it will be exhausted.  After that there is a stepped discount and an estimate of the effective price including any savings plan discount can be used.