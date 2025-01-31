# Tenire Framework Documentation

*Disclaimer: This is a work in progress and the documentation and actual project are not yet complete.*

## Overview
The Tenire Framework is a way to make an LLM act. Inspired by, and building on top of, the Browser_Use project, it is a way to make an LLM experience limited agency. In order to summit this lofty peak, the framework is built with a modular architecture that allows for easy integration of new components and services. Through a healthy mix of complex and simple innovation, the framework is designed to be robust, scalable, and flexible.

**Note: The use case chosen was a gambling chatbot for recursive bet strategies to test the framework. The flexibility of the program proves that the framework is capable of handling a wide range of use cases in the future.**

## Table of Contents

### Core Systems
1. [Architecture](architecture/system_architecture.md)
2. [RAG System Overview](rag/overview.md)
3. [Concurrency](concurrency/system_architecture.md)
4. [Organizers](organizers/overview.md)
5. [Public Services](public_services/overview.md)

### Architecture
2. [Architecture](architecture/)
   - [Overview](architecture/overview.md)
   - [Initialization Flow](architecture/initialization/flow.md)
   - [System Architecture Paper](architecture/system_architecture_paper.md)
   - [Components](architecture/components/overview.md)
   - [Paper](architecture/system_architecture_paper.md)

### Concurrency
3. [Concurrency](concurrency/)
   - [Novel Management Architecture](concurrency/system_architecture.md)
   - [Event Loop](concurrency/event_loop.md)
   - [Paper](concurrency/concurrency.md)

### RAG
4. [RAG](rag/)
   - [Overview](rag/overview.md)
   - [Docustore](rag/pipeline/docustore.md)
   - [Retriever](rag/pipeline/retriever.md)
   - [Embeddings](rag/pipeline/embeddings.md)
   - [Paper](rag/rag_paper.md)

### Organizers
5. [Organizers](organizers/)
   - [Overview](organizers/overview.md)
   - [Compactor](organizers/compactor.md)
   - [Monitor](organizers/monitor.md)
   - [Scheduler](organizers/scheduler.md)

### Public Services
5. [Public Services](public_services/)
   - [Overview](public_services/overview.md)
   - [Hospital](public_services/hospital/overview.md)

## Getting Started
To understand the framework, start with the [Architecture](architecture/overview.md) and then follow the [Initialization Flow](architecture/initialization/flow.md) documentation. It will get easier the more you read. But my honest advice, run it and have fun.
