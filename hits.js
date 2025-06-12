  // Example array of timestamps (ISO 8601 format)
    const timestamps = [
"2025-06-09T18:09:35",
"2025-06-09T17:36:43",
"2025-06-09T13:09:55",
"2025-06-09T10:21:19",
"2025-06-09T10:03:06",
"2025-06-09T09:17:47",
"2025-06-09T05:15:07",
"2025-06-08T20:09:36",
"2025-06-08T17:51:02",
"2025-06-08T11:33:40",
"2025-06-07T12:40:17",
"2025-06-07T01:02:28",
"2025-06-06T16:32:01",
"2025-06-06T15:37:55",
"2025-06-05T20:59:16",
"2025-06-05T06:12:15",
"2025-06-04T20:08:36",
"2025-06-04T11:53:30",
"2025-06-04T11:37:33",
"2025-06-04T09:06:17",
"2025-06-04T08:50:38",
"2025-06-04T00:46:54",
"2025-06-03T22:38:24",
"2025-06-03T22:32:01",
"2025-06-03T20:29:26",
"2025-06-03T17:06:04",
"2025-06-03T14:34:05",
"2025-06-03T13:50:24",
"2025-06-03T13:21:10",
"2025-06-03T13:18:30",
"2025-06-03T13:16:59",
"2025-06-03T12:49:20",
"2025-06-03T12:42:14",
"2025-06-03T12:31:44",
"2025-06-03T12:25:33",
"2025-06-02T16:54:36",
"2025-05-31T07:19:11",
"2025-05-31T07:06:08",
"2025-05-26T08:39:46",
"2025-05-25T15:42:00",
"2025-05-24T03:09:12",
"2025-05-23T06:52:24",
"2025-05-23T03:43:06",
"2025-05-23T02:03:55",
"2025-05-22T18:26:44",
"2025-05-22T15:33:32",
"2025-05-22T02:05:38",
"2025-05-22T01:44:33",
"2025-05-22T01:14:33"
    ];
    // Parse to Date objects
    const dates = timestamps.map(d => new Date(d));

    // Histogram binning function (by day)
    const histogram = d3.histogram()
      .value(d => d)
      .domain(d3.extent(dates))
      .thresholds(d3.timeDay.range(
        d3.timeDay.floor(d3.min(dates)),
        d3.timeDay.ceil(d3.max(dates))
      ));

    const bins = histogram(dates);

    // Setup SVG
    const svg = d3.select("svg");
    const width = +svg.attr("width");
    const height = +svg.attr("height");
    const margin = {top: 20, right: 30, bottom: 30, left: 40};

    const x = d3.scaleTime()
      .domain(d3.extent(dates))
      .range([margin.left, width - margin.right]);

    const y = d3.scaleLinear()
      .domain([0, d3.max(bins, d => d.length)]).nice()
      .range([height - margin.bottom, margin.top]);

    // Draw bars
    svg.append("g")
      .selectAll("rect")
      .data(bins)
      .join("rect")
        .attr("x", d => x(d.x0))
        .attr("y", d => y(d.length))
        .attr("width", d => x(d.x1) - x(d.x0) - 1)
        .attr("height", d => y(0) - y(d.length))
        .attr("fill", "steelblue");

// X Axis (labels centered on bars)
svg.append("g")
  .attr("transform", `translate(0,${height - margin.bottom})`)
  .call(
    d3.axisBottom(x)
      .tickValues(bins.map(d => new Date((d.x0.getTime() + d.x1.getTime()) / 2))) // midpoint
      .tickFormat(d3.timeFormat('%b %d'))
  );
// Y Axis label (vertical)
svg.append("text")
  .attr("text-anchor", "middle")
  .attr("transform", `rotate(-90)`)
  .attr("x", -height / 2)
  .attr("y", 15)
  .style("fill", "white")

  .attr("dy",2)
  .text("Hits per day");
// Y Axis (with integer ticks only)
svg.append("g")
  .attr("transform", `translate(${margin.left},0)`)
  .call(
    d3.axisLeft(y)
      .ticks(d3.max(bins, d => d.length)) // Set number of ticks to max count
      .tickFormat(d => Number.isInteger(d) ? d : "") // Only show integers
  );

  // Add chart title
svg.append("text")
  .attr("x", width / 2)
  .attr("y", 20)
  .attr("text-anchor", "middle")
  .style("font-size", "16px")
  .style("font-weight", "bold")
    .style("fill", "white")

  .text("Hits in last 2 weeks");
