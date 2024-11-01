Peter Grecian \- CURRICULUM VITAE  
 Location: Surbiton, Surrey.  
peter.grecian at gmail.com

*Skills: Linux, terraform, python, puppet, AWS, OpenSearch, Github Actions.*

Working in the fields of AI research and film and TV CGI I have proposed, built and operated CPU/GPU clusters, including networking, air cooling, power distribution and software installation.  These cutting edge fields require the ability to exploit the latest hardware to provide novel applications using bulk computing, high speed networks and clustered storage.  

2018 \- 2024 **The BMJ**.   AWS Cloud Engineer/DevOps.  
The British Medical Journal is one of the most prestigious publications in its field.  The BMJ publishes 60 journals in paper and digital form and a clinical decision support tool called "Best Practice" which is the product I have been most closely associated with,.   

Initially the app was unreliable and would alert the operator on call most nights, via Pager Duty.  Sometimes that would be me.  I installed an ELK stack, and traced the issue to a mixture of EC2 instances having insufficient memory, Xmx being set incorrectly for the Java processes and in one case a Java library which had a memory leak.

Having achieved stability I could then right size the EC2 instances and move to ARM architecture to save money and carbon emissions.  Current effort is to migrate to Kubernetes using EKS, Kustomise and github actions/ArgoCD for the many benefits that brings.

Provisioning is with terraform, EC2 configuration with puppet.  I've written applications for the developers to start up dev and stage stacks "on demand", and to stop instances at weekends etc using lambda, python, flask and dynamoDB.   

I am expert at OpenSearch Dashboards and have presented talks on how to use this powerful tool, and I maintain fluentbit/logstash ingestion into the OpenSearch domain I call "The Logstore".  I often help developers with queries and searches.

I am the person at the BMJ people go to for AWS cost estimation and planning and have presented talks to my colleagues on this topic.

I also use prometheus, Jenkins, AppDynamics, grafana.  Cloudwatch alerts.

**Jan 2015 \- Mar 2018: Crystal Ski (TUI holidays).  Senior Cloud Engineer/DevOps**  
Responsible for providing AWS hosting of a website which sold 100s of millions of pounds of skiing holidays each season.  Puppet, terraform, Jenkins.

**Feb 2014 \- Jan 2015:  Method Studios, 8-14 Meard St, London.  Senior Systems Engineer** for a Hollywood special effects company.  High speed bulk data handling using Isilon storage, DST tape and 10GE switches.

**June 2012 \- Feb 2014: Google DeepMind.  40 Bernard St, London. Senior Linux Systems Administrator** for a rapidly expanding startup company purchased whilst I was there by Google.  Design, purchase and installation of a high power Linux compute cluster with Grid Engine job scheduling for neural network training.  I was expert on the HP/Supermicro Intel product lines and how to get the most compute power in the smallest space.  Working with researchers of international renown.

**Jan 2008 \- June 2012 Freelance hardware engineer and systems administrator.**     
Designing building and selling an all in one storage and network solution for mid size boutique visual effects companies in and around Soho, using 10GigE and RAID controllers.

**Aug 1994-Jan 2008,  Senior Systems and programming lead, MPC, 127 Wardour St, London**.  Providing bespoke hardware and C++ software solutions for TV advertising and broadcast and film clients.  Developed real time hardware rendered particle systems for National Lottery commercials and Harry Potter Films.  Working with film directors and advertising agencies.

Prior to working in TV and film I worked at Philips Research on Gallium Arsenide transistors for 6 years, publishing papers in journals and presenting a paper at a symposium.

**Links**  
[https://www.imdb.com/name/nm0337418](https://www.imdb.com/name/nm0337418)  
[https://www.linkedin.com/in/peter-grecian-1700a317](https://www.linkedin.com/in/peter-grecian-1700a317)  
[https://w3.petergrecian.co.uk/contents](https://w3.petergrecian.co.uk/contents)  
[https://github.com/PeterGrecian](https://github.com/PeterGrecian)  
[http://adsabs.harvard.edu/abs/1989ElL....25..871B](http://adsabs.harvard.edu/abs/1989ElL....25..871B) GaAs camel-cathode Gunn devices

   
**Education**:  
MSc IT and Computer Graphics, Middlesex University  
BSc (Hons) Physics (3rd), Oxford University, St Catherine's College.  Excelled at the practical experimental course.
