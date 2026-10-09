"""Hand-authored practice items for the demo course (Computer Networks), used ONLY as a last resort when the model
cannot produce valid items. Written by the project team (original wording); keyed by topic slug."""
from __future__ import annotations

from app.llm.schemas import PracticeItemDraft as I

BANK: dict[str, list[I]] = {
    "layering": [
        I(kind="mcq", prompt="Which layer adds a header containing source and destination port numbers?",
          options=["Transport layer", "Network layer", "Link layer", "Application layer"], answer_key="Transport layer",
          distractor_tags={"Network layer": "confuses_ports_with_ip", "Link layer": "confuses_ports_with_mac",
                           "Application layer": "wrong_layer"}),
        I(kind="short_text", prompt="In your own words, what is encapsulation in a layered protocol stack?",
          answer_key="Each layer adds its own header to the data it receives from the layer above as the data moves down the stack.",
          rubric="Mentions: header, layer, data"),
    ],
    "tcp-reliability": [
        I(kind="mcq", prompt="In the TCP three-way handshake, what does the server send in reply to the client's SYN?",
          options=["SYN-ACK", "ACK only", "FIN", "RST"], answer_key="SYN-ACK",
          distractor_tags={"ACK only": "skips_server_syn", "FIN": "confuses_open_and_close", "RST": "wrong_segment_type"}),
        I(kind="short_text", prompt="How does TCP detect and recover from a lost segment?",
          answer_key="The sender waits for an acknowledgment; if its retransmission timer expires first it retransmits the segment.",
          rubric="Mentions: acknowledgment, timer, retransmit"),
    ],
    "tcp-flow-control": [
        I(kind="mcq", prompt="What does the receive window (rwnd) advertise to the sender?",
          options=["The free buffer space at the receiver", "The sender's congestion window", "The link bandwidth",
                   "The number of router hops"], answer_key="The free buffer space at the receiver",
          distractor_tags={"The sender's congestion window": "confuses_flow_and_congestion_control"}),
        I(kind="short_text", prompt="What is the difference between flow control and congestion control in TCP?",
          answer_key="Flow control protects the receiver's buffer; congestion control protects the network between the hosts.",
          rubric="Mentions: receiver, network, buffer"),
    ],
    "tcp-congestion": [
        I(kind="mcq", prompt="In TCP Reno, what happens after three duplicate acknowledgments?",
          options=["Fast retransmit followed by fast recovery", "Slow start from 1 MSS", "The connection is reset",
                   "ssthresh is doubled"], answer_key="Fast retransmit followed by fast recovery",
          distractor_tags={"Slow start from 1 MSS": "confuses_timeout_and_dup_acks",
                           "ssthresh is doubled": "wrong_ssthresh_update"}),
        I(kind="numeric", prompt="A sender starts slow start with cwnd = 1 MSS and no loss occurs. What is cwnd, in MSS, after 4 round-trip times?",
          answer_key="16", numeric_tolerance=0.0),
        I(kind="short_text", prompt="Why does the window stop doubling when cwnd reaches ssthresh?",
          answer_key="At ssthresh TCP switches from slow start to congestion avoidance, where cwnd grows linearly (about one MSS per round-trip time).",
          rubric="Mentions: ssthresh, congestion avoidance, linear"),
    ],
    "ip-addressing": [
        I(kind="numeric", prompt="How many usable host addresses does a /26 IPv4 subnet contain?", answer_key="62", numeric_tolerance=0.0),
        I(kind="numeric", prompt="How many /26 subnets fit inside one /24 network?", answer_key="4", numeric_tolerance=0.0),
        I(kind="mcq", prompt="What is the subnet mask of a /24 network?",
          options=["255.255.255.0", "255.255.0.0", "255.255.255.128", "255.0.0.0"], answer_key="255.255.255.0",
          distractor_tags={"255.255.0.0": "miscounts_prefix_bits", "255.255.255.128": "miscounts_prefix_bits"}),
    ],
    "routing": [
        I(kind="mcq", prompt="Which algorithm do link-state routers run to compute shortest paths?",
          options=["Dijkstra's algorithm", "Bellman-Ford", "Kruskal's algorithm", "Quicksort"],
          answer_key="Dijkstra's algorithm", distractor_tags={"Bellman-Ford": "confuses_link_state_and_distance_vector"}),
        I(kind="short_text", prompt="What is the count-to-infinity problem?",
          answer_key="In distance vector routing, after a link failure routers can keep increasing their cost estimates through each other, forming a loop that converges slowly.",
          rubric="Mentions: distance vector, loop, cost"),
    ],
}
