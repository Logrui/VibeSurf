import asyncio
import copy
import json
import logging
import os
import pdb
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import pickle
from typing import Any, Dict, List, Literal, Optional
from uuid_extensions import uuid7str
from collections import defaultdict
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, Field
from json_repair import repair_json

from browser_use.browser.session import BrowserSession
from browser_use.llm.base import BaseChatModel
from browser_use.llm.messages import UserMessage, SystemMessage, BaseMessage, AssistantMessage
from browser_use.browser.views import TabInfo

from vibe_surf.agents.browser_use_agent import BrowserUseAgent
from vibe_surf.agents.report_writer_agent import ReportWriterAgent
from vibe_surf.agents.views import CustomAgentOutput

from vibe_surf.agents.prompts.vibe_surf_prompt import (
    VIBESURF_SYSTEM_PROMPT,
)
from vibe_surf.browser.browser_manager import BrowserManager
from vibe_surf.tools.browser_use_tools import BrowserUseTools
from vibe_surf.tools.vibesurf_tools import VibeSurfTools
from vibe_surf.tools.file_system import CustomFileSystem

from vibe_surf.logger import get_logger

logger = get_logger(__name__)


class BrowserTaskResult(BaseModel):
    """Result from browser task execution"""
    agent_id: str
    task: str
    success: bool
    result: Optional[str] = None
    error: Optional[str] = None
    screenshots: List[str] = Field(default_factory=list)
    extracted_data: Optional[str] = None


class ControlResult(BaseModel):
    """Result of a control operation"""
    success: bool
    message: str
    timestamp: datetime = Field(default_factory=datetime.now)
    details: Optional[Dict[str, Any]] = None


class AgentStatus(BaseModel):
    """Status of an individual agent"""
    agent_id: str
    status: Literal["running", "paused", "stopped", "idle", "error"] = "idle"
    current_action: Optional[str] = None
    last_update: datetime = Field(default_factory=datetime.now)
    error_message: Optional[str] = None
    pause_reason: Optional[str] = None


class VibeSurfStatus(BaseModel):
    """Overall status of the vibesurf execution"""
    overall_status: Literal["running", "paused", "stopped", "idle", "error"] = "idle"
    agent_statuses: Dict[str, AgentStatus] = Field(default_factory=dict)
    progress: Dict[str, Any] = Field(default_factory=dict)
    last_update: datetime = Field(default_factory=datetime.now)
    active_step: Optional[str] = None


@dataclass
class VibeSurfState:
    """Simplified LangGraph state for VibeSurfAgent workflow"""

    # Core task information
    original_task: str = ""
    upload_files: List[str] = field(default_factory=list)
    session_id: str = field(default_factory=lambda: uuid7str())

    # Workflow state
    current_step: str = "vibesurf_agent"
    is_complete: bool = False

    # Current action and parameters from LLM
    current_action: Optional[str] = None
    action_params: Optional[Dict[str, Any]] = None

    # Browser task execution
    browser_tasks: List[Dict[str, Any]] = field(default_factory=list)
    browser_results: List[BrowserTaskResult] = field(default_factory=list)

    # Response outputs
    generated_report_path: Optional[str] = None
    final_response: Optional[str] = None

    # vibesurf_agent
    vibesurf_agent: Optional[Any] = None

    # Control state management
    paused: bool = False
    stopped: bool = False
    should_pause: bool = False
    should_stop: bool = False


def format_browser_results(browser_results: List[BrowserTaskResult]) -> str:
    """Format browser results for LLM prompt"""
    result_text = []
    for result in browser_results:
        status = "✅ Success" if result.success else "❌ Failed"
        result_text.append(f"{status}: {result.task}")
        if result.result:
            result_text.append(f"  Result: {result.result}...")
        if result.error:
            result_text.append(f"  Error: {result.error}")
    return "\n".join(result_text)


def log_agent_activity(state: VibeSurfState, agent_name: str, agent_status: str, agent_msg: str) -> None:
    """Log agent activity to the activity log"""
    activity_entry = {
        "agent_name": agent_name,
        "agent_status": agent_status,  # working, result, error
        "agent_msg": agent_msg
    }
    state.activity_logs.append(activity_entry)
    logger.info(f"📝 Logged activity: {agent_name} - {agent_status}:\n{agent_msg}")


def create_browser_agent_step_callback(state: VibeSurfState, agent_name: str):
    """Create a step callback function for browser-use agent to log each step"""

    def step_callback(browser_state_summary, agent_output, step_num: int) -> None:
        """Callback function to log browser agent step information"""
        try:
            # Format step information as markdown
            step_msg = f"## Step {step_num}\n\n"

            # Add thinking if present
            if agent_output.thinking:
                step_msg += f"**💡 Thinking:**\n{agent_output.thinking}\n\n"

            # Add evaluation if present
            if agent_output.evaluation_previous_goal:
                step_msg += f"**👍 Evaluation:**\n{agent_output.evaluation_previous_goal}\n\n"

            # Add memory if present
            # if agent_output.memory:
            #     step_msg += f"**🧠 Memory:** {agent_output.memory}\n\n"

            # Add next goal if present
            if agent_output.next_goal:
                step_msg += f"**🎯 Next Goal:**\n{agent_output.next_goal}\n\n"

            # Add action summary
            if agent_output.action and len(agent_output.action) > 0:
                action_count = len(agent_output.action)
                step_msg += f"**⚡ Actions:**\n"

                # Add brief action details
                for i, action in enumerate(agent_output.action):  # Limit to first 3 actions to avoid too much detail
                    action_data = action.model_dump(exclude_unset=True)
                    action_name = next(iter(action_data.keys())) if action_data else 'unknown'
                    action_params = json.dumps(action_data[action_name],
                                               ensure_ascii=False) if action_name in action_data else ""
                    step_msg += f"- [x] {action_name}: {action_params}\n"
            else:
                step_msg += f"**⚡ Actions:** No actions\n"

            # Log the step activity
            log_agent_activity(state, agent_name, "working", step_msg.strip())

        except Exception as e:
            logger.error(f"❌ Error in step callback for {agent_name}: {e}")
            # Log a simple fallback message
            log_agent_activity(state, agent_name, "step", f"Step {step_num} completed")

    return step_callback


# Control-aware node wrapper
async def control_aware_node(node_func, state: VibeSurfState, node_name: str) -> VibeSurfState:
    """
    Wrapper for workflow nodes that adds control state checking
    """
    # Check control state before executing node
    if state.stopped:
        logger.info(f"🛑 Node {node_name} skipped - workflow stopped")
        return state

    # Handle pause state
    while state.paused or state.should_pause:
        if not state.paused and state.should_pause:
            logger.info(f"⏸️ Node {node_name} pausing workflow")
            state.paused = True
            state.should_pause = False
            # Note: control_timestamps removed in simplified state

        logger.debug(f"⏸️ Node {node_name} waiting - workflow paused")
        await asyncio.sleep(0.5)  # Check every 500ms

        # Allow stopping while paused
        if state.stopped or state.should_stop:
            logger.info(f"🛑 Node {node_name} stopped while paused")
            state.stopped = True
            state.should_stop = False
            # Note: control_timestamps removed in simplified state
            return state

    # Check for stop signal
    if state.should_stop:
        logger.info(f"🛑 Node {node_name} stopping workflow")
        state.stopped = True
        state.should_stop = False
        # Note: control_timestamps removed in simplified state
        return state

    # Execute the actual node
    logger.debug(f"▶️ Executing node: {node_name}")
    # Note: last_control_action removed in simplified state

    try:
        return await node_func(state)
    except Exception as e:
        logger.error(f"❌ Node {node_name} failed: {e}")
        raise


# LangGraph Nodes

async def vibesurf_agent_node(state: VibeSurfState) -> VibeSurfState:
    """
    Main VibeSurf agent node using thinking + action pattern like report_writer_agent
    """
    return await control_aware_node(_vibesurf_agent_node_impl, state, "vibesurf_agent")


def format_browser_tabs(tabs: Optional[List[TabInfo]] = None) -> str:
    if not tabs:
        return ""
    return "\n".join([f"[{i}] Page Title: {item.title}, Page Url: {item.url}, Page ID: {item.target_id}" for i, item in
                      enumerate(tabs)])


async def _vibesurf_agent_node_impl(state: VibeSurfState) -> VibeSurfState:
    """Implementation using thinking + action pattern similar to report_writer_agent"""
    logger.info("🎯 VibeSurf Agent: Processing with thinking + action pattern...")

    # Create action model and agent output using VibeSurfTools
    ActionModel = state.tools.registry.create_action_model()
    AgentOutput = CustomAgentOutput.type_with_custom_actions(ActionModel)

    # Get current browser context
    browser_tabs = await state.browser_manager.main_browser_session.get_tabs()
    browser_tabs_md = format_browser_tabs(browser_tabs)
    active_browser_tab = await state.browser_manager.get_activate_tab()
    active_tab_md = ""
    if active_browser_tab:
        active_tab_md = f"Page Title: {active_browser_tab.title}, Page Url: {active_browser_tab.url}, Page ID: {active_browser_tab.target_id}"

    # Format context information
    context_info = []
    if browser_tabs_md:
        context_info.append(f"Available Browser Tabs:\n{browser_tabs_md}")
    if active_tab_md:
        context_info.append(f"Current Active Browser Tab:\n{active_tab_md}")
    if state.browser_results:
        results_md = format_browser_results(state.browser_results)
        context_info.append(f"Previous Browser Results:\n{results_md}")
    if state.generated_report_path:
        context_info.append(f"Generated Report Path: {state.generated_report_path}")

    context_str = "\n\n".join(context_info) if context_info else "No additional context available."

    # Add context to message history
    if context_info:
        state.message_history.append(UserMessage(content=context_str))

    try:
        # Get LLM response with action output format
        response = await state.llm.ainvoke(state.message_history, output_format=AgentOutput)
        parsed = response.completion
        actions = parsed.action

        # Add assistant message to history
        state.message_history.append(AssistantMessage(content=response.completion))

        # Log thinking if present
        if hasattr(parsed, 'thinking') and parsed.thinking:
            log_agent_activity(state, "vibesurf_agent", "thinking", parsed.thinking)

        # Process actions
        for i, action in enumerate(actions):
            action_data = action.model_dump(exclude_unset=True)
            action_name = next(iter(action_data.keys())) if action_data else 'unknown'
            logger.info(f"🛠️ Processing action {i + 1}/{len(actions)}: {action_name}")

            # Check for special routing actions
            if action_name == 'execute_browser_use_agent_tasks':
                # Route to browser task execution node
                params = action_data[action_name]
                state.browser_tasks = params.get('tasks', [])
                state.current_action = 'execute_browser_use_agent_tasks'
                state.action_params = params
                state.current_step = "browser_task_execution"
                log_agent_activity(state, "vibesurf_agent", "result",
                                   f"Routing to browser task execution with {len(state.browser_tasks)} tasks")
                return state

            elif action_name == 'execute_report_writer_agent':
                # Route to report task execution node
                params = action_data[action_name]
                state.current_action = 'execute_report_writer_agent'
                state.action_params = params
                state.current_step = "report_task_execution"
                log_agent_activity(state, "vibesurf_agent", "result", "Routing to report generation")
                return state

            elif action_name == 'task_done':
                # Handle response/completion - direct to END
                params = action_data[action_name]
                response_content = params.get('response', 'Task completed')
                follow_tasks = params.get('suggestion_follow_tasks', [])

                # Format final response
                final_response = f"{response_content}"
                if follow_tasks:
                    final_response += "\n\n## Suggested Follow-up Tasks:\n"
                    for j, task in enumerate(follow_tasks[:3], 1):
                        final_response += f"{j}. {task}\n"

                state.final_response = final_response
                state.is_complete = True
                log_agent_activity(state, "vibesurf_agent", "result", final_response)
                return state

            else:
                # Execute regular action using tools with shared file system
                # For todo-related actions, read todo.md and log activity
                if action_name in ['generate_todos', 'read_todos', 'modify_todos']:
                    try:
                        # Try to read existing todo.md
                        todo_content = await state.file_system.read_file('todo.md')
                        log_agent_activity(state, "vibesurf_agent", "working", f"{todo_content}")
                    except Exception as e:
                        pass

                result = await state.tools.act(
                    action=action,
                    browser_manager=state.browser_manager,
                    llm=state.llm,
                    file_system=state.file_system,
                )

                # Add result to message history
                if result.extracted_content:
                    state.message_history.append(UserMessage(content=f'Action result:\n{result.extracted_content}'))
                    log_agent_activity(state, "vibesurf_agent", "result", result.extracted_content)

                if result.error:
                    state.message_history.append(UserMessage(content=f'Action error:\n{result.error}'))
                    log_agent_activity(state, "vibesurf_agent", "error", result.error)

        return state

    except Exception as e:
        logger.error(f"❌ VibeSurf agent failed: {e}")
        state.final_response = f"Task execution failed: {str(e)}"
        state.is_complete = True
        log_agent_activity(state, "vibesurf_agent", "error", f"Agent failed: {str(e)}")
        return state


async def browser_task_execution_node(state: VibeSurfState) -> VibeSurfState:
    """
    Execute browser tasks assigned by supervisor agent
    """
    return await control_aware_node(_browser_task_execution_node_impl, state, "browser_task_execution")


async def _browser_task_execution_node_impl(state: VibeSurfState) -> VibeSurfState:
    """Implementation of browser task execution node - simplified tab-based approach"""
    logger.info("🚀 Executing browser tasks assigned by vibesurf agent...")

    # Log agent activity
    log_agent_activity(state, "browser_task_executor", "working",
                       f"Executing {len(state.browser_tasks)} browser tasks")

    try:
        # Execute tasks using simplified tab-based approach
        results = await execute_tab_based_browser_tasks(state)

        # Update browser results
        state.browser_results.extend(results)

        # Return to vibesurf agent for next decision
        state.current_step = "vibesurf_agent"

        # Log result
        successful_tasks = sum(1 for result in results if result.success)
        log_agent_activity(state, "browser_task_executor", "result",
                           f"Browser execution completed: {successful_tasks}/{len(results)} tasks successful")

        logger.info(f"✅ Browser task execution completed with {len(results)} results")
        return state

    except Exception as e:
        logger.error(f"❌ Browser task execution failed: {e}")

        # Create error results for browser tasks
        error_results = []
        for i, task in enumerate(state.browser_tasks):
            # Get the actual task description for the error result
            if isinstance(task, dict):
                task_description = task.get('description', 'Unknown task')
                tab_id = task.get('tab_id')
            else:
                task_description = str(task)
                tab_id = None

            error_results.append(BrowserTaskResult(
                agent_id="error",
                task=task_description,
                success=False,
                error=str(e)
            ))

        state.browser_results.extend(error_results)
        state.current_step = "vibesurf_agent"

        log_agent_activity(state, "browser_task_executor", "error", f"Browser execution failed: {str(e)}")
        return state


async def report_task_execution_node(state: VibeSurfState) -> VibeSurfState:
    """
    Execute HTML report generation task assigned by supervisor agent
    """
    return await control_aware_node(_report_task_execution_node_impl, state, "report_task_execution")


async def _report_task_execution_node_impl(state: VibeSurfState) -> VibeSurfState:
    """Implementation of report task execution node"""
    logger.info("📄 Executing HTML report generation task...")

    # Log agent activity
    log_agent_activity(state, "report_task_executor", "working", "Generating HTML report")

    try:
        # Use ReportWriterAgent to generate HTML report
        report_writer = ReportWriterAgent(
            llm=state.llm,
            workspace_dir=state.task_dir
        )

        report_data = {
            "original_task": state.original_task,
            "execution_results": state.browser_results,
            "report_type": "detailed",  # Default to detailed report
            "upload_files": state.upload_files
        }

        report_path = await report_writer.generate_report(report_data)

        state.generated_report_path = report_path

        # Return to vibesurf agent for next decision
        state.current_step = "vibesurf_agent"

        log_agent_activity(state, "report_task_executor", "result",
                           f"HTML report generated successfully at: `{report_path}`")

        logger.info(f"✅ Report generated: {report_path}")
        return state

    except Exception as e:
        logger.error(f"❌ Report generation failed: {e}")
        state.current_step = "vibesurf_agent"
        log_agent_activity(state, "report_task_executor", "error", f"Report generation failed: {str(e)}")
        return state


async def execute_tab_based_browser_tasks(state: VibeSurfState) -> List[BrowserTaskResult]:
    """Execute browser tasks - parallel execution for multiple tasks, single for single task"""
    task_count = len(state.browser_tasks)
    logger.info(f"🔄 Executing {task_count} browser tasks...")

    if task_count <= 1:
        # Single task execution
        logger.info("📝 Using single execution for single task")
        return await execute_single_browser_tasks(state)
    else:
        # Multiple tasks execution - parallel approach
        logger.info(f"🚀 Using parallel execution for {task_count} tasks")
        return await execute_parallel_browser_tasks(state)


async def execute_parallel_browser_tasks(state: VibeSurfState) -> List[BrowserTaskResult]:
    """Execute pending tasks in parallel using multiple browser agents"""
    logger.info("🔄 Executing pending tasks in parallel...")

    # Register agents with browser manager
    agents = []
    pending_tasks = state.browser_tasks
    bu_agent_ids = []
    register_sessions = []
    for i, task in enumerate(pending_tasks):
        agent_id = f"agent-{i + 1}-{state.task_id[-4:]}"
        if isinstance(task, list):
            target_id, task_description = task
        elif isinstance(task, dict):
            task_description = task.get('description', 'Unknown task')
            target_id = task.get('tab_id')
        else:
            task_description = str(task)
            target_id = None
        register_sessions.append(
            state.browser_manager.register_agent(agent_id, target_id=target_id)
        )
        bu_agent_ids.append(agent_id)
    agent_browser_sessions = await asyncio.gather(*register_sessions)

    for i, task in enumerate(pending_tasks):
        agent_id = f"agent-{i + 1}-{state.task_id[-4:]}"
        if isinstance(task, list):
            target_id, task_description = task
        elif isinstance(task, dict):
            task_description = task.get('description', 'Unknown task')
            target_id = task.get('tab_id')
        else:
            task_description = str(task)
        try:
            # Log agent creation
            log_agent_activity(state, f"browser_use_agent-{i + 1}-{state.task_id[-4:]}", "working",
                               f"{task_description}")

            # Create BrowserUseAgent for each task
            if state.upload_files:
                upload_files_md = format_upload_files_list(state.upload_files)
                bu_task = task_description + f"\nAvailable uploaded files:\n{upload_files_md}\n"
            else:
                bu_task = task_description

            # Create step callback for this agent
            agent_name = f"browser_use_agent-{i + 1}-{state.task_id[-4:]}"
            step_callback = create_browser_agent_step_callback(state, agent_name)

            agent = BrowserUseAgent(
                task=bu_task,
                llm=state.llm,
                browser_session=agent_browser_sessions[i],
                controller=state.vibesurf_agent,
                task_id=f"{state.task_id}-{i + 1}",
                file_system_path=os.path.join(state.task_dir, f"{state.task_id}-{i + 1}"),
                register_new_step_callback=step_callback,
                extend_system_message="Please make sure the language of your output in JSON value should remain the same as the user's request or task.",
            )
            agents.append(agent)

            # Track agent in VibeSurfAgent for control coordination
            if state.vibesurf_agent and hasattr(state.vibesurf_agent, '_running_agents'):
                state.vibesurf_agent._running_agents[agent_id] = agent
                logger.debug(f"🔗 Registered parallel agent {agent_id} for control coordination")

        except Exception as e:
            logger.error(f"❌ Failed to create agent {agent_id}: {e}")
            log_agent_activity(state, f"browser_use_agent-{i + 1}-{state.task_id[-4:]}", "error",
                               f"Failed to create agent: {str(e)}")

    # Execute all agents in parallel
    try:
        histories = await asyncio.gather(*[agent.run() for agent in agents], return_exceptions=True)

        # Process results
        results = []
        for i, (agent, history) in enumerate(zip(agents, histories)):
            agent_id = f"agent-{i + 1}-{state.task_id[-4:]}"
            if isinstance(history, Exception):
                results.append(BrowserTaskResult(
                    agent_id=f"agent-{i + 1}",
                    task=pending_tasks[i],
                    success=False,
                    error=str(history)
                ))
                # Log error
                log_agent_activity(state, f"browser_use_agent-{i + 1}-{state.task_id[-4:]}", "error",
                                   f"Task failed: {str(history)}")
            else:
                results.append(BrowserTaskResult(
                    agent_id=f"agent-{i + 1}",
                    task=pending_tasks[i],
                    success=history.is_successful(),
                    result=history.final_result() if hasattr(history, 'final_result') else "Task completed",
                    error=str(history.errors()) if history.has_errors() and not history.is_successful() else ""
                ))
                # Log result
                if history.is_successful():
                    result_text = history.final_result() if hasattr(history, 'final_result') else "Task completed"
                    log_agent_activity(state, f"browser_use_agent-{i + 1}-{state.task_id[-4:]}", "result",
                                       f"Task completed successfully: \n{result_text}")
                else:
                    error_text = str(history.errors()) if history.has_errors() else "Unknown error"
                    log_agent_activity(state, f"browser_use_agent-{i + 1}-{state.task_id[-4:]}", "error",
                                       f"Task failed: {error_text}")

        return results

    finally:
        # Remove agents from control tracking and cleanup browser sessions
        for i, agent_id in enumerate(bu_agent_ids):
            if not isinstance(pending_tasks[i], list):
                await state.browser_manager.unregister_agent(agent_id, close_tabs=True)
            if state.vibesurf_agent and hasattr(state.vibesurf_agent, '_running_agents'):
                state.vibesurf_agent._running_agents.pop(agent_id, None)
                logger.debug(f"🔗 Unregistered parallel agent {agent_id} from control coordination")


async def execute_single_browser_tasks(state: VibeSurfState) -> List[BrowserTaskResult]:
    """Execute pending tasks in single mode one by one"""
    logger.info("🔄 Executing pending tasks in single mode...")

    results = []
    for i, task in enumerate(state.browser_tasks):
        if isinstance(task, list):
            target_id, task_description = task
            await state.browser_manager.main_browser_session.get_or_create_cdp_session(target_id, focus=True)
        elif isinstance(task, dict):
            task_description = task.get('description', 'Unknown task')
            tab_id = task.get('tab_id')
            if tab_id:
                await state.browser_manager.main_browser_session.get_or_create_cdp_session(tab_id, focus=True)
            else:
                await state.browser_manager.get_activate_tab()
        else:
            task_description = str(task)
            await state.browser_manager.get_activate_tab()
        logger.info(f"🔄 Executing task ({i + 1}/{len(state.browser_tasks)}): {task_description}")

        agent_id = f"agent-single-{state.task_id[-4:]}-{i}"

        # Log agent activity
        log_agent_activity(state, f"browser_use_agent-{state.task_id[-4:]}", "working", f"{task_description}")
        try:
            if state.upload_files:
                upload_files_md = format_upload_files_list(state.upload_files)
                bu_task = task_description + f"\nAvailable user uploaded files:\n{upload_files_md}\n"
            else:
                bu_task = task_description
            # Create step callback for this agent
            agent_name = f"browser_use_agent-{state.task_id[-4:]}"
            step_callback = create_browser_agent_step_callback(state, agent_name)

            agent = BrowserUseAgent(
                task=bu_task,
                llm=state.llm,
                browser_session=state.browser_manager.main_browser_session,
                controller=state.vibesurf_agent,
                task_id=f"{state.task_id}-{i}",
                file_system_path=os.path.join(state.task_dir, f"{state.task_id}-{i}"),
                register_new_step_callback=step_callback,
                extend_system_message="Please make sure the language of your output in JSON values should remain the same as the user's request or task."
            )

            # Track agent in VibeSurfAgent for control coordination
            if state.vibesurf_agent and hasattr(state.vibesurf_agent, '_running_agents'):
                state.vibesurf_agent._running_agents[agent_id] = agent
                logger.debug(f"🔗 Registered single agent {agent_id} for control coordination")

            try:
                history = await agent.run()

                result = BrowserTaskResult(
                    agent_id=agent_id,
                    task=task,
                    success=history.is_successful(),
                    result=history.final_result() if hasattr(history, 'final_result') else "Task completed",
                    error=str(history.errors()) if history.has_errors() and not history.is_successful() else ""
                )

                # Log result
                if result.success:
                    log_agent_activity(state, f"browser_use_agent-{state.task_id[-4:]}", "result",
                                       f"Task completed successfully: \n{result.result}")
                else:
                    log_agent_activity(state, f"browser_use_agent-{state.task_id[-4:]}", "error",
                                       f"Task failed: {result.error}")

                results.append(result)
            finally:
                # Remove agent from control tracking
                if state.vibesurf_agent and hasattr(state.vibesurf_agent, '_running_agents'):
                    state.vibesurf_agent._running_agents.pop(agent_id, None)
                    logger.debug(f"🔗 Unregistered single agent {agent_id} from control coordination")

        except Exception as e:
            logger.error(f"❌ Single task execution failed: {e}")
            log_agent_activity(state, f"browser_use_agent-{state.task_id[-4:]}", "error",
                               f"Task execution failed: {str(e)}")
            results.append(BrowserTaskResult(
                agent_id=agent_id,
                task=task,
                success=False,
                error=str(e)
            ))

    return results


def route_after_vibesurf_agent(state: VibeSurfState) -> str:
    """Route based on vibesurf agent decisions"""
    if state.current_step == "browser_task_execution":
        return "browser_task_execution"
    elif state.current_step == "report_task_execution":
        return "report_task_execution"
    elif state.current_step == "vibesurf_agent":
        return "vibesurf_agent"  # Continue in vibesurf agent loop
    elif state.is_complete:
        return "END"  # task_done sets is_complete=True, go directly to END
    else:
        return "END"  # Default fallback - complete workflow


def route_after_browser_task_execution(state: VibeSurfState) -> str:
    """Route back to vibesurf agent after browser task completion"""
    return "vibesurf_agent"


def route_after_report_task_execution(state: VibeSurfState) -> str:
    """Route back to vibesurf agent after report task completion"""
    return "vibesurf_agent"


def should_continue(state: VibeSurfState) -> str:
    """Main continuation logic"""
    if state.is_complete:
        return "END"
    else:
        return "continue"


def create_vibe_surf_workflow() -> StateGraph:
    """Create the simplified LangGraph workflow with supervisor agent as core tools"""

    workflow = StateGraph(VibeSurfState)

    # Add nodes for simplified architecture
    workflow.add_node("vibesurf_agent", vibesurf_agent_node)
    workflow.add_node("browser_task_execution", browser_task_execution_node)
    workflow.add_node("report_task_execution", report_task_execution_node)

    # Set entry point
    workflow.set_entry_point("vibesurf_agent")

    # VibeSurf agent routes to different execution nodes or END
    workflow.add_conditional_edges(
        "vibesurf_agent",
        route_after_vibesurf_agent,
        {
            "browser_task_execution": "browser_task_execution",
            "report_task_execution": "report_task_execution",
            "vibesurf_agent": "vibesurf_agent",
            "END": END
        }
    )

    # Execution nodes return to vibesurf agent
    workflow.add_conditional_edges(
        "browser_task_execution",
        route_after_browser_task_execution,
        {
            "vibesurf_agent": "vibesurf_agent"
        }
    )

    workflow.add_conditional_edges(
        "report_task_execution",
        route_after_report_task_execution,
        {
            "vibesurf_agent": "vibesurf_agent"
        }
    )

    return workflow


class VibeSurfAgent:
    """Main LangGraph-based VibeSurf Agent"""

    def __init__(
            self,
            llm: BaseChatModel,
            browser_manager: BrowserManager,
            tools: VibeSurfTools,
            workspace_dir: str = "./workspace",
            thinking_mode: bool = True
    ):
        """Initialize VibeSurfAgent with required components"""
        self.llm: BaseChatModel = llm
        self.browser_manager: BrowserManager = browser_manager
        self.tools: VibeSurfTools = tools
        self.workspace_dir = workspace_dir
        os.makedirs(self.workspace_dir, exist_ok=True)
        self.thinking_mode = thinking_mode

        self.cur_session_id = None
        self.file_system: Optional[CustomFileSystem] = None
        self.message_history = []
        self.activity_logs = []

        # Create LangGraph workflow
        self.workflow = create_vibe_surf_workflow()
        self.app = self.workflow.compile()

        # Control state management
        self._control_lock = asyncio.Lock()
        self._current_state: Optional[VibeSurfState] = None
        self._running_agents: Dict[str, Any] = {}  # Track running BrowserUseAgent instances
        self._execution_task: Optional[asyncio.Task] = None

        logger.info("🌊 VibeSurf Agent initialized with LangGraph workflow")

    def load_message_history(self, session_id: Optional[str] = None) -> list:
        """Load message history for a specific session, or return [] for new sessions"""
        if session_id is None:
            return []

        session_message_history_path = os.path.join(self.workspace_dir, "sessions", session_id, "message_history.pkl")

        if not os.path.exists(session_message_history_path):
            logger.info(f"No message history found for session {session_id}, creating new")
            return []

        try:
            with open(session_message_history_path, "rb") as f:
                message_history = pickle.load(f)
                logger.info(f"Loading message history for session {session_id} from {session_message_history_path}")
                return message_history
        except Exception as e:
            logger.error(f"Failed to load message history for session {session_id}: {e}")
            return []

    def save_message_history(self, session_id: Optional[str] = None):
        """Save message history for a specific session"""
        if session_id is None:
            return

        # Create session directory if it doesn't exist
        session_dir = os.path.join(self.workspace_dir, "sessions", session_id)
        os.makedirs(session_dir, exist_ok=True)

        session_message_history_path = os.path.join(session_dir, "message_history.pkl")

        try:
            with open(session_message_history_path, "wb") as f:
                logger.info(f"Saving message history for session {session_id} to {session_message_history_path}")
                pickle.dump(self.message_history, f)
        except Exception as e:
            logger.error(f"Failed to save message history for session {session_id}: {e}")

    def load_activity_logs(self, session_id: Optional[str] = None) -> list:
        """Load activity logs for a specific session, or return [] for new sessions"""
        if session_id is None:
            return []

        session_activity_logs_path = os.path.join(self.workspace_dir, "sessions", session_id, "activity_logs.pkl")

        if not os.path.exists(session_activity_logs_path):
            logger.info(f"No activity logs found for session {session_id}, creating new")
            return []

        try:
            with open(session_activity_logs_path, "rb") as f:
                activity_logs = pickle.load(f)
                logger.info(f"Loading activity logs for session {session_id} from {session_activity_logs_path}")
                return activity_logs
        except Exception as e:
            logger.error(f"Failed to load activity logs for session {session_id}: {e}")
            return []

    def save_activity_logs(self, session_id: Optional[str] = None):
        """Save activity logs for a specific session"""
        if session_id is None:
            return

        # Create session directory if it doesn't exist
        session_dir = os.path.join(self.workspace_dir, "sessions", session_id)
        os.makedirs(session_dir, exist_ok=True)

        session_activity_logs_path = os.path.join(session_dir, "activity_logs.pkl")

        try:
            with open(session_activity_logs_path, "wb") as f:
                logger.info(f"Saving activity logs for session {session_id} to {session_activity_logs_path}")
                pickle.dump(self.activity_logs, f)
        except Exception as e:
            logger.error(f"Failed to save activity logs for session {session_id}: {e}")

    async def stop(self, reason: str = None) -> ControlResult:
        """
        Stop the vibesurf execution immediately
        
        Args:
            reason: Optional reason for stopping
            
        Returns:
            ControlResult with operation status
        """
        try:
            async with self._control_lock:
                reason = reason or "Manual stop requested"
                logger.info(f"🛑 Stopping agent execution: {reason}")

                self.message_history.append(UserMessage(
                    content=f"🛑 Stopping agent execution: {reason}"))

                if self._current_state:
                    self._current_state.should_stop = True
                    self._current_state.stopped = True

                # Stop all running agents with timeout
                try:
                    await asyncio.wait_for(self._stop_all_agents(reason), timeout=3.0)
                except asyncio.TimeoutError:
                    logger.warning("⚠️ Agent stopping timed out, continuing with task cancellation")

                # Cancel execution task if running
                if self._execution_task and not self._execution_task.done():
                    self._execution_task.cancel()
                    try:
                        await asyncio.wait_for(self._execution_task, timeout=2.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        logger.debug("🛑 Execution task cancelled or timed out")

                logger.info(f"✅ VibeSurf execution stopped: {reason}")
                return ControlResult(
                    success=True,
                    message=f"VibeSurf stopped successfully: {reason}",
                    details={"reason": reason}
                )

        except asyncio.TimeoutError:
            error_msg = f"Stop operation timed out after 10 seconds"
            logger.error(error_msg)
            return ControlResult(
                success=False,
                message=error_msg,
                details={"timeout": True}
            )
        except Exception as e:
            error_msg = f"Failed to stop VibeSurf: {str(e)}"
            logger.error(error_msg)
            return ControlResult(
                success=False,
                message=error_msg,
                details={"error": str(e)}
            )

    async def pause(self, reason: str = None) -> ControlResult:
        """
        Pause the VibeSurf execution
        
        Args:
            reason: Optional reason for pausing
            
        Returns:
            ControlResult with operation status
        """
        async with self._control_lock:
            try:
                reason = reason or "Manual pause requested"
                logger.info(f"⏸️ Pausing agent execution: {reason}")

                self.message_history.append(UserMessage(
                    content=f"⏸️ Pausing agent execution: {reason}"))

                if self._current_state:
                    self._current_state.should_pause = True

                # Pause all running agents
                await self._pause_all_agents(reason)

                logger.info(f"✅ VibeSurf execution paused: {reason}")
                return ControlResult(
                    success=True,
                    message=f"VibeSurf paused successfully: {reason}",
                    details={"reason": reason}
                )

            except Exception as e:
                error_msg = f"Failed to pause VibeSurf: {str(e)}"
                logger.error(error_msg)
                return ControlResult(
                    success=False,
                    message=error_msg,
                    details={"error": str(e)}
                )

    async def resume(self, reason: str = None) -> ControlResult:
        """
        Resume the VibeSurf execution
        
        Args:
            reason: Optional reason for resuming
            
        Returns:
            ControlResult with operation status
        """
        async with self._control_lock:
            try:
                reason = reason or "Manual resume requested"
                logger.info(f"▶️ Resuming agent execution: {reason}")

                self.message_history.append(UserMessage(
                    content=f"▶️ Resuming agent execution: {reason}"))

                if self._current_state:
                    self._current_state.paused = False
                    self._current_state.should_pause = False
                    # Note: control_timestamps, control_reasons, last_control_action removed in simplified state

                # Resume all paused agents
                await self._resume_all_agents(reason)

                logger.info(f"✅ VibeSurf execution resumed: {reason}")
                return ControlResult(
                    success=True,
                    message=f"VibeSurf resumed successfully: {reason}",
                    details={"reason": reason}
                )

            except Exception as e:
                error_msg = f"Failed to resume VibeSurf: {str(e)}"
                logger.error(error_msg)
                return ControlResult(
                    success=False,
                    message=error_msg,
                    details={"error": str(e)}
                )

    async def pause_agent(self, agent_id: str, reason: str = None) -> ControlResult:
        """
        Pause a specific agent
        
        Args:
            agent_id: ID of the agent to pause
            reason: Optional reason for pausing
            
        Returns:
            ControlResult with operation status
        """
        async with self._control_lock:
            try:
                reason = reason or f"Manual pause requested for agent {agent_id}"
                logger.info(f"⏸️ Pausing agent {agent_id}: {reason}")

                # Pause the specific agent if it's running
                agent = self._running_agents.get(agent_id)
                if agent:
                    if hasattr(agent, 'pause'):
                        await agent.pause()
                        logger.info(f"✅ Agent {agent_id} paused successfully")
                else:
                    logger.warning(f"⚠️ Agent {agent_id} not found")

                return ControlResult(
                    success=True,
                    message=f"Agent {agent_id} paused successfully: {reason}",
                    details={"agent_id": agent_id, "reason": reason}
                )

            except Exception as e:
                error_msg = f"Failed to pause agent {agent_id}: {str(e)}"
                logger.error(error_msg)
                return ControlResult(
                    success=False,
                    message=error_msg,
                    details={"agent_id": agent_id, "error": str(e)}
                )

    async def resume_agent(self, agent_id: str, reason: str = None) -> ControlResult:
        """
        Resume a specific agent
        
        Args:
            agent_id: ID of the agent to resume
            reason: Optional reason for resuming
            
        Returns:
            ControlResult with operation status
        """
        async with self._control_lock:
            try:
                reason = reason or f"Manual resume requested for agent {agent_id}"
                logger.info(f"▶️ Resuming agent {agent_id}: {reason}")

                # Resume the specific agent if it's running
                agent = self._running_agents.get(agent_id)
                if agent:
                    if hasattr(agent, 'resume'):
                        await agent.resume()
                        logger.info(f"✅ Agent {agent_id} resumed successfully")
                else:
                    logger.warning(f"⚠️ Agent {agent_id} not found")

                return ControlResult(
                    success=True,
                    message=f"Agent {agent_id} resumed successfully: {reason}",
                    details={"agent_id": agent_id, "reason": reason}
                )

            except Exception as e:
                error_msg = f"Failed to resume agent {agent_id}: {str(e)}"
                logger.error(error_msg)
                return ControlResult(
                    success=False,
                    message=error_msg,
                    details={"agent_id": agent_id, "error": str(e)}
                )

    def get_status(self) -> VibeSurfStatus:
        """
        Get current status of the VibeSurf and all agents
        
        Returns:
            VibeSurfStatus with current state information
        """
        try:
            # Determine overall status
            if not self._current_state:
                overall_status = "idle"
            elif self._current_state.stopped:
                overall_status = "stopped"
            elif self._current_state.paused or self._current_state.should_pause:
                overall_status = "paused"
            elif self._current_state.is_complete:
                overall_status = "completed"
            else:
                overall_status = "running"

            # Build agent statuses
            agent_statuses = {}

            # Add status for tracked running agents
            for agent_id, agent in self._running_agents.items():
                status = "running"
                current_action = None
                error_message = None
                pause_reason = None

                # Simplified status checking since paused_agents removed
                if self._current_state and self._current_state.stopped:
                    status = "stopped"
                elif self._current_state and self._current_state.paused:
                    status = "paused"

                # Get current action if available
                if agent and hasattr(agent, 'state'):
                    try:
                        if hasattr(agent.state, 'last_result') and agent.state.last_result:
                            current_action = f"Last action completed"
                        else:
                            current_action = "Executing task"
                    except:
                        current_action = "Unknown"

                agent_statuses[agent_id] = AgentStatus(
                    agent_id=agent_id,
                    status=status,
                    current_action=current_action,
                    error_message=error_message,
                    pause_reason=pause_reason
                )

            # Build progress information
            progress = {}
            if self._current_state:
                progress = {
                    "current_step": self._current_state.current_step,
                    "is_complete": self._current_state.is_complete,
                    "browser_tasks_count": len(self._current_state.browser_tasks),
                    "browser_results_count": len(self._current_state.browser_results)
                }

            return VibeSurfStatus(
                overall_status=overall_status,
                agent_statuses=agent_statuses,
                progress=progress,
                active_step=self._current_state.current_step if self._current_state else None
            )

        except Exception as e:
            logger.error(f"❌ Failed to get status: {e}")
            return VibeSurfStatus(
                overall_status="error",
                agent_statuses={},
                progress={"error": str(e)}
            )

    async def _stop_all_agents(self, reason: str) -> None:
        """Stop all running agents"""
        for agent_id, agent in self._running_agents.items():
            try:
                # Also try to pause if available as a fallback
                if agent and hasattr(agent, 'stop'):
                    await agent.stop()
                    logger.info(f"⏸️ stop agent {agent_id}")
            except Exception as e:
                logger.warning(f"⚠️ Failed to stop agent {agent_id}: {e}")

    async def _pause_all_agents(self, reason: str) -> None:
        """Pause all running agents"""
        for agent_id, agent in self._running_agents.items():
            try:
                if hasattr(agent, 'pause'):
                    await agent.pause()
                    logger.info(f"⏸️ Paused agent {agent_id}")
                    # Note: paused_agents removed in simplified state
            except Exception as e:
                logger.warning(f"⚠️ Failed to pause agent {agent_id}: {e}")

    async def _resume_all_agents(self, reason: str) -> None:
        """Resume all paused agents"""
        for agent_id, agent in self._running_agents.items():
            try:
                if hasattr(agent, 'resume'):
                    await agent.resume()
                    logger.info(f"▶️ Resumed agent {agent_id}")
                    # Note: paused_agents removed in simplified state
            except Exception as e:
                logger.warning(f"⚠️ Failed to resume agent {agent_id}: {e}")

    async def process_upload_files(self, upload_files: Optional[List[str]] = None):
        new_upload_files = []
        for ufile_path in upload_files:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            dst_filename = f"uploads/{timestamp}-{os.path.basename(ufile_path)}"
            await self.file_system.copy_file(ufile_path, dst_filename, external_src_file=True)
            new_upload_files.append(dst_filename)
        return new_upload_files

    def format_upload_files(self, upload_files: Optional[List[str]] = None, use_abspath: bool = False) -> str:
        """Format uploaded file for LLM prompt"""
        if upload_files is None:
            return ""
        if use_abspath:
            file_urls = []
            for i, file_path in enumerate(upload_files):
                abs_file_path = self.file_system.get_absolute_path(file_path)
                normalized_path = abs_file_path.replace(os.path.sep, '/')
                file_url = f"{i + 1}. [{os.path.basename(file_path)}](file:///{normalized_path})"
                file_urls.append(file_url)
            return "\n".join(file_urls)
        else:
            return "\n".join(
                [f"{i + 1}. {file_path}" for i, file_path in enumerate(upload_files)])

    async def run(
            self,
            task: str,
            upload_files: Optional[List[str]] = None,
            session_id: Optional[str] = None,
            thinking_mode: bool = True
    ) -> str | None:
        """
        Main execution method that returns markdown summary with control capabilities
        
        Args:
            task: User task to execute
            upload_files: Optional list of file paths that user has uploaded
            
        Returns:
            str: Markdown summary of execution results
        """
        logger.info(f"🚀 Starting VibeSurfAgent execution for task: {task}")
        agent_activity_logs = []
        try:
            self.thinking_mode = thinking_mode
            session_id = session_id or self.cur_session_id or uuid7str()
            if session_id != self.cur_session_id:
                # Load session-specific data when switching sessions
                self.cur_session_id = session_id
                self.message_history = self.load_message_history(session_id)
                self.activity_logs = self.load_activity_logs(session_id)
                session_dir = os.path.join(self.workspace_dir, "sessions", self.cur_session_id)
                os.makedirs(session_dir, exist_ok=True)
                self.file_system = CustomFileSystem(session_dir)

            if upload_files and not isinstance(upload_files, list):
                upload_files = [upload_files]
            upload_files = await self.process_upload_files(upload_files)

            if not self.message_history:
                self.message_history.append(SystemMessage(content=VIBESURF_SYSTEM_PROMPT))

            # Format processed upload files for prompt
            upload_files_md = self.format_upload_files(upload_files)
            user_request = f"* User's New Request:\n{task}\n"
            if upload_files:
                user_request += f"* User Uploaded Files:\n{upload_files_md}\n"
            self.message_history.append(
                UserMessage(content=user_request)
            )
            logger.info(user_request)

            abs_upload_files_md = self.format_upload_files(upload_files, use_abspath=True)
            activity_entry = {
                "agent_name": 'user',
                "agent_status": 'request',  # working, result, error
                "agent_msg": f"{task}\nUpload Files:\n{abs_upload_files_md}\n" if upload_files else f"{task}"
            }
            self.activity_logs.append(activity_entry)

            # Initialize state first (needed for file processing)
            initial_state = VibeSurfState(
                original_task=task,
                upload_files=upload_files or [],
                session_id=session_id,
                vibesurf_agent=self,
            )

            # Set current state for control operations
            async with self._control_lock:
                self._current_state = initial_state
                self._running_agents.clear()  # Clear any previous agents

            async def _execute_workflow():
                """Internal workflow execution with proper state management"""
                try:
                    # Run without checkpoints
                    logger.info("🔄 Executing LangGraph workflow...")
                    return await self.app.ainvoke(initial_state)
                finally:
                    # Clean up running agents
                    async with self._control_lock:
                        self._running_agents.clear()

            # Execute workflow as a task for control management
            self._execution_task = asyncio.create_task(_execute_workflow())
            final_state = await self._execution_task

            # Update current state reference
            async with self._control_lock:
                self._current_state = final_state

            # Get final result
            result = await self._get_result(final_state)
            logger.info("✅ VibeSurfAgent execution completed")
            return result

        except asyncio.CancelledError:
            logger.info("🛑 VibeSurfAgent execution was cancelled")
            # Add cancellation activity log
            if agent_activity_logs:
                activity_entry = {
                    "agent_name": "VibeSurfAgent",
                    "agent_status": "cancelled",
                    "agent_msg": "Task execution was cancelled by user request."
                }
                agent_activity_logs.append(activity_entry)
            return f"# Task Execution Cancelled\n\n**Task:** {task}\n\nExecution was stopped by user request."
        except Exception as e:
            logger.error(f"❌ VibeSurfAgent execution failed: {e}")
            # Add error activity log
            if agent_activity_logs:
                activity_entry = {
                    "agent_name": "VibeSurfAgent",
                    "agent_status": "error",
                    "agent_msg": f"Task execution failed: {str(e)}"
                }
                agent_activity_logs.append(activity_entry)
            return f"# Task Execution Failed\n\n**Task:** {task}\n\n**Error:** {str(e)}\n\nPlease try again or contact support."
        finally:
            if agent_activity_logs:
                activity_entry = {
                    "agent_name": "VibeSurfAgent",
                    "agent_status": "done",  # working, result, error
                    "agent_msg": "Finish Task."
                }
                agent_activity_logs.append(activity_entry)
            # Save session-specific data
            if self.cur_session_id:
                self.save_message_history(self.cur_session_id)
                self.save_activity_logs(self.cur_session_id)
            async with self._control_lock:
                self._current_state = None
                self._execution_task = None
                self._running_agents.clear()

    def get_activity_logs(self, session_id: Optional[str] = None, message_index: Optional[int] = None) -> Optional[
        List[Dict]]:
        if session_id is None:
            session_id = self.cur_session_id

        # Ensure session_id exists in activity_logs
        if session_id not in self.activity_logs:
            logger.warning(
                f"⚠️ Session {session_id} not found in activity_logs. Available sessions: {list(self.activity_logs.keys())}")
            return None

        session_logs = self.activity_logs[session_id]
        logger.debug(f"📋 Session {session_id} has {len(session_logs)} activity logs")

        if message_index is None:
            logger.debug(f"📤 Returning all {len(session_logs)} activity logs for session {session_id}")
            return session_logs
        else:
            if message_index >= len(session_logs):
                logger.debug(
                    f"⚠️ Message index {message_index} out of range for session {session_id} (max index: {len(session_logs) - 1})")
                return None
            else:
                activity_log = session_logs[message_index]
                logger.debug(
                    f"📤 Returning activity log at index {message_index}: {activity_log.get('agent_name', 'unknown')} - {activity_log.get('agent_status', 'unknown')}")
                return activity_log

    async def _get_result(self, state) -> str:
        """Get the final result from execution with simplified workflow support"""
        # Handle both dict and dataclass state types due to LangGraph serialization
        final_response = state.get('final_response') if isinstance(state, dict) else getattr(state, 'final_response',
                                                                                             None)
        original_task = state.get('original_task') if isinstance(state, dict) else getattr(state, 'original_task',
                                                                                           'Unknown task')
        if final_response:
            return final_response
        else:
            return f"# Task Execution Completed\n\n**Task:** {original_task}\n\nTask execution completed but no detailed result available."


workflow = create_vibe_surf_workflow()
