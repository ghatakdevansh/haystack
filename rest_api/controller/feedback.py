from typing import Optional
import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import Dict, Union, List

router = APIRouter()

from .search import PIPELINE

logger = logging.getLogger(__name__)

#TODO make this generic for other pipelines with different naming
retriever = PIPELINE.get_node(name="ESRetriever")
document_store = retriever.document_store

retriever = PIPELINE.get_node(name="DPRRetriever")
faiss_document_store = retriever.document_store

DB_INDEX_FEEDBACK = "label"

class FAQQAFeedback(BaseModel):
    question: str = Field(..., description="The question input by the user, i.e., the query.")
    is_correct_answer: bool = Field(..., description="Whether the answer is correct or not.")
    document_id: str = Field(..., description="The document in the query result for which feedback is given.")
    model_id: Optional[int] = Field(None, description="The model used for the query.")


class DocQAFeedback(FAQQAFeedback):
    is_correct_document: bool = Field(
        ...,
        description="In case of negative feedback, there could be two cases; incorrect answer but correct "
        "document & incorrect document. This flag denotes if the returned document was correct.",
    )
    answer: str = Field(..., description="The answer string.")
    offset_start_in_doc: int = Field(
        ..., description="The answer start offset in the original doc. Only required for doc-qa feedback."
    )

class FilterRequest(BaseModel):
    filters: Optional[Dict[str, Optional[Union[str, List[str]]]]] = None

@router.post("/feedback")
def doc_qa_feedback(feedback: DocQAFeedback):
    document_store.write_labels([{"origin": "user-feedback", **feedback.dict()}])

@router.post("/eval-feedback")
def eval_doc_qa_feedback(filters: FilterRequest = None):
    """
    Return basic accuracy metrics based on the user feedback.
    Which ratio of answers was correct? Which ratio of documents was correct?
    You can supply filters in the request to only use a certain subset of labels.

    **Example:**

            ```
                | curl --location --request POST 'http://127.0.0.1:8000/eval-doc-qa-feedback' \
                | --header 'Content-Type: application/json' \
                | --data-raw '{ "filters": {"document_id": ["XRR3xnEBCYVTkbTystOB"]} }'
    """

    if filters:
        filters = filters.filters
        filters["origin"] = ["user-feedback"]
    else:
        filters = {"origin": ["user-feedback"]}

    labels = document_store.get_all_labels(
        index=DB_INDEX_FEEDBACK,
        filters=filters
    )

    if len(labels) > 0:
        answer_feedback = [1 if l.is_correct_answer else 0 for l in labels]
        doc_feedback = [1 if l.is_correct_document else 0 for l in labels]

        answer_accuracy = sum(answer_feedback)/len(answer_feedback)
        doc_accuracy = sum(doc_feedback)/len(doc_feedback)

        res = {"answer_accuracy": answer_accuracy,
               "document_accuracy": doc_accuracy,
               "n_feedback": len(labels)}
    else:
        res = {"answer_accuracy": None,
               "document_accuracy": None,
               "n_feedback": 0}
    return res

@router.get("/export-feedback")
def export_doc_qa_feedback(context_size: int = 100_000, full_document_context=True, only_positive_labels=False):
    """
    SQuAD format JSON export for question/answer pairs that were marked as "relevant".

    The context_size param can be used to limit response size for large documents.
    """
    if only_positive_labels:
        labels = document_store.get_all_labels(
            index=DB_INDEX_FEEDBACK, filters={"is_correct_answer": [True], "origin": ["user-feedback"]}
        )
    else:
        labels = document_store.get_all_labels(
            index=DB_INDEX_FEEDBACK, filters={"origin": ["user-feedback"]}
        )
        # Filter out the labels where the passage is correct but answer is wrong (in SQuAD this matches neither a "positive example" nor a negative "is_impossible" one)
        labels = [l for l in labels if not (l.is_correct_document==True and l.is_correct_answer==False)]

    export_data = []
    c = 0
    for label in labels:
        print(c)
        c += 1
        document = document_store.get_document_by_id(label.document_id)
        if document is None:
            document = faiss_document_store.get_document_by_id(label.document_id)
            if document is None:
                logger.error("Could not find document with id {label.document_id} for label id {label.id}")

        if label.is_correct_answer == False and label.is_correct_document == False:
            label.no_answer = True
        else:
            label.no_answer = False

        if full_document_context:
            squad_label = {"paragraphs": [{
                "context": document.text,
                "id": label.document_id,
                "qas": [
                    {"question": label.question,
                     "id": label.id,
                     "is_impossible": label.no_answer,
                     "answers": [{
                         "text": label.answer,
                         "answer_start": label.offset_start_in_doc
                     }]
                     }
                ]
            }
            ]
            }

            # quality check
            start = squad_label["paragraphs"][0]["qas"][0]["answers"][0]["answer_start"]
            answer = squad_label["paragraphs"][0]["qas"][0]["answers"][0]["text"]
            context = squad_label["paragraphs"][0]["context"]
            if not context[start:start + len(answer)] == answer:
                logger.error(
                    f"Skipping invalid squad label as string via offsets ('{context[start:start + len(answer)]}') does not match answer string ('{answer}') ")
            export_data.append(squad_label)
        else:
            raise NotImplementedError()
            # # TODO fix offsets for context and return  proper Squad format!
            # text = document.text
            #
            # # the final length of context(including the answer string) is 'context_size'.
            # # we try to add equal characters for context before and after the answer string.
            # # if either beginning or end of text is reached, we correspondingly
            # # append more context characters at the other end of answer string.
            # context_to_add = int((context_size - len(label.answer)) / 2)
            #
            # start_pos = max(label.offset_start_in_doc - context_to_add, 0)
            # additional_context_at_end = max(context_to_add - label.offset_start_in_doc, 0)
            #
            # end_pos = min(label.offset_start_in_doc + len(label.answer) + context_to_add, len(text) - 1)
            # additional_context_at_start = max(label.offset_start_in_doc + len(label.answer) + context_to_add - len(text), 0)
            #
            # start_pos = max(0, start_pos - additional_context_at_start)
            # end_pos = min(len(text) - 1, end_pos + additional_context_at_end)
            #
            # context_to_export = text[start_pos:end_pos]
            #
            # # squad_label = [label]
            # # squad_label["answers"] = [{"text": label.answer, "answer_start": label.}]
            # export_data.append({"paragraphs": [{"qas": label, "context": context_to_export}]})

    export = {"data": export_data}

    return export


